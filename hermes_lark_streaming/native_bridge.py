"""Turn ownership and native-host events for the existing CardKit controller.

This module deliberately does not import Hermes: contracts can be exercised with
the same controller and transport doubles as the established rendering tests.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .card_mutations import MUTATION_SCOPE, CardMutations
from .controller import StreamCardController
from .feishu import FeishuClient
from .native_context import native_home
from .native_history import DeliveryHistory
from .streaming.segments import SegmentType
from .streaming.text import strip_reasoning_tags


@dataclass
class NativeTurn:
    message_id: str
    session_key: str
    chat_id: str
    anchor_id: str | None
    owner_ids: frozenset[str]
    started_at: float = field(default_factory=time.monotonic)
    turn_id: str = ""
    session_id: str = ""
    model: str = ""
    api_mode: str = ""
    terminal: bool = False
    failed: bool = False
    interrupted: bool = False
    stop_command: bool = False
    fallback: bool = False
    completed: bool = False
    native_fallback_sent: bool = False
    answer: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    requests: set[str] = field(default_factory=set)
    context: dict[str, Any] = field(default_factory=dict)
    snapshots: dict[str, str] = field(default_factory=dict)
    card_message_id: str = ""
    card_entity_id: str = ""
    delivery_uncertain: bool = False
    pending_inputs: set[str] = field(default_factory=set)
    iteration: int = 0
    closed_iterations: set[int] = field(default_factory=set)
    answer_segments: dict[int, Any] = field(default_factory=dict)
    reasoning_segments: dict[int, Any] = field(default_factory=dict)
    expected_answers: dict[int, str] = field(default_factory=dict)
    stream_iterations: dict[str, int] = field(default_factory=dict)
    segment_order: dict[int, tuple[int, int]] = field(default_factory=dict)
    native_tool_prefixes: set[str] = field(default_factory=set)


class NativeBridge:
    """Event-loop-owned bridge; host observer threads only schedule ``observe``."""

    def __init__(self, controller: StreamCardController) -> None:
        self.controller = controller
        registry = getattr(controller, "_card_mutations", None)
        self._mutation_registry = registry if isinstance(registry, CardMutations) else None
        self._mutation_epoch = self._mutation_registry.epoch if self._mutation_registry is not None else 0
        self._profile_home = controller._profile_home if hasattr(controller, "_profile_home") else None
        self.stopping = False
        self.finished = DeliveryHistory()
        self.on_completed: Callable[[NativeTurn], None] | None = None
        self.turns: dict[str, NativeTurn] = {}
        self.messages: dict[str, NativeTurn] = {}
        self.lock = asyncio.Lock()
        self._queue: deque[tuple[str, str, dict[str, Any]]] = deque()
        self._queue_lock = threading.Lock()
        self._drain_lock = asyncio.Lock()

    def enqueue(self, kind: str, key: str, payload: dict[str, Any]) -> None:
        if self.stopping:
            return
        with self._queue_lock:
            self._queue.append((kind, key, payload))

    @native_home
    async def drain(self) -> None:
        async with self._drain_lock:
            while True:
                with self._queue_lock:
                    if not self._queue:
                        return
                    kind, key, payload = self._queue.popleft()
                async with self.lock:
                    await self.observe(kind, key, payload)

    def admit(self, event: Any, session_key: str) -> NativeTurn:
        source = event.source
        message_id = str(event.message_id or "")
        existing = self.messages.get(message_id)
        if existing is not None:
            return existing
        turn = NativeTurn(
            message_id=message_id,
            session_key=session_key,
            chat_id=str(source.chat_id),
            anchor_id=getattr(event, "reply_anchor_override", None) or event.reply_to_message_id or message_id or None,
            owner_ids=frozenset(str(v) for v in (source.user_id, source.user_id_alt) if v),
        )
        if self.finished.get(message_id) is not None:
            turn.completed = turn.terminal = True
            return turn
        self.turns[session_key] = turn
        self.messages[message_id] = turn
        return turn

    def owns_transport(self) -> bool:
        registry = self._mutation_registry
        if registry is None:
            return True
        scope = MUTATION_SCOPE.get()
        return self._mutation_epoch == registry.epoch and (
            scope is None or scope == (id(registry), registry.epoch)
        )

    def route(self, session_key: str, payload: dict[str, Any] | None = None) -> NativeTurn | None:
        if not self.owns_transport():
            return None
        inbound = str((payload or {}).get("_inbound_id") or "")
        turn = self.messages.get(inbound) if inbound else self.turns.get(session_key)
        if turn is None or turn.completed:
            return None
        if turn.session_key != session_key:
            return None
        if self.turns.get(session_key) is not turn:
            return None
        if payload:
            turn_id = str(payload.get("turn_id") or "")
            session_id = str(payload.get("session_id") or "")
            # Child agents inherit the route context, but have their own turn ids.
            if turn.turn_id and turn_id and turn.turn_id != turn_id:
                return None
            if turn.session_id and session_id and turn.session_id != session_id:
                return None
            turn.turn_id = turn.turn_id or turn_id
            turn.session_id = turn.session_id or session_id
        return turn

    async def ensure_card(self, turn: NativeTurn) -> bool:
        if not self.owns_transport() or turn.completed or turn.fallback or not self.controller.enabled:
            return False
        existing = self.controller._sessions.get(turn.message_id)
        if existing is not None:
            existing.last_activity_at = time.time()
        self.controller.on_message_started(
            message_id=turn.message_id,
            chat_id=turn.chat_id,
            anchor_id=turn.anchor_id,
            session_key=turn.session_key,
        )
        session = self.controller._sessions.get(turn.message_id)
        if session is None or not await self.controller._wait_for_card_creation(session):
            turn.fallback = True
            return False
        if session.state.is_terminal or not session.has_card:
            turn.fallback = True
            return False
        turn.card_message_id = str(session.card_msg_id or "")
        turn.card_entity_id = str(session.card_id or "")
        return bool(turn.card_message_id)

    @native_home
    async def frame(self, turn: NativeTurn, text: str, *, stream_id: str, finalize: bool) -> bool:
        """Accept cumulative edit snapshots; only a host turn-end may seal a card."""
        await self.drain()
        async with self.lock:
            if not await self.ensure_card(turn):
                return False
            if turn.iteration:
                iteration = turn.stream_iterations.get(stream_id)
                if iteration is None:
                    available = [
                        key
                        for key, expected in turn.expected_answers.items()
                        if expected.strip() and key not in turn.stream_iterations.values()
                    ]
                    iteration = min(available, default=turn.iteration)
                    turn.stream_iterations[stream_id] = iteration
                self.replace_answer(turn, iteration, text)
                return True
            previous = turn.snapshots.get(stream_id, "")
            if text.startswith(previous):
                suffix = text[len(previous) :]
                if suffix:
                    self.controller.on_answer(message_id=turn.message_id, text=suffix)
            elif text != previous:
                # The host may replace a speculative draft with corrected final text.
                # Replace this stream's current answer instead of appending both copies.
                session = self.controller._sessions[turn.message_id]
                state = session.segment_state
                answer = (
                    next(
                        (s for s in reversed(state.segments) if s.type == SegmentType.ANSWER),
                        None,
                    )
                    if state
                    else None
                )
                if answer is not None:
                    answer.text = text
                    answer.dirty = True
                    self.controller._schedule_flush(session)
                elif text:
                    self.controller.on_answer(message_id=turn.message_id, text=text)
            turn.snapshots[stream_id] = text
            turn.answer = text
            return True

    @native_home
    async def complete(self, turn: NativeTurn) -> bool:
        if turn.completed:
            return not turn.fallback
        retired = not self.owns_transport()
        if retired:
            ok = False
            turn.answer = ""
        elif turn.interrupted:
            ok = await self.controller.on_session_aborted(session_key=turn.session_key, stop_command=turn.stop_command)
        else:
            ok = await self.controller.on_completed_wait(
                message_id=turn.message_id,
                answer=turn.answer,
                is_error=turn.failed,
                model=turn.model,
                duration=time.monotonic() - turn.started_at,
                tokens={**turn.usage, "api_calls": len(turn.requests)},
                context=turn.context,
            )
        client = self.controller._client
        if isinstance(client, FeishuClient) and isinstance(client.mutations, CardMutations):
            doc = client.mutations.documents.get(turn.card_entity_id)
            turn.delivery_uncertain = bool(
                doc and (doc.unconfirmed or doc.write_uncertain or any(m is None for m in doc.messages))
            )
        if turn.delivery_uncertain:
            ok = False
        turn.completed = True
        turn.fallback = not ok
        turn.expected_answers.clear()
        turn.answer_segments.clear()
        turn.reasoning_segments.clear()
        turn.snapshots.clear()
        turn.segment_order.clear()
        turn.native_tool_prefixes.clear()
        turn.stream_iterations.clear()
        if turn.interrupted:
            turn.answer = ""
        turn.requests.clear()
        turn.context.clear()
        turn.closed_iterations.clear()
        turn.pending_inputs.clear()
        if not retired:
            self.controller.consume_text_fallback(turn.message_id)
        self.finished.add(turn.message_id, turn.chat_id, "inbound")
        if self.messages.get(turn.message_id) is turn:
            self.messages.pop(turn.message_id, None)
        if self.turns.get(turn.session_key) is turn:
            self.turns.pop(turn.session_key, None)
        if self.on_completed is not None:
            self.on_completed(turn)
        return bool(ok)

    def replace_answer(self, turn: NativeTurn, iteration: int, text: str) -> None:
        text = strip_reasoning_tags(text)
        segment = turn.answer_segments.get(iteration)
        session = self.controller._sessions.get(turn.message_id)
        if session is None or session.segment_state is None:
            return
        if segment is not None and segment in session.segment_state.segments:
            segment.text, segment.dirty = text, True
            self.controller._schedule_flush(session)
        elif text:
            if session.segment_state.segments and session.segment_state.segments[-1].type == SegmentType.ANSWER:
                session.segment_state._new_answer("")
            self.controller.on_answer(message_id=turn.message_id, text=text)
            turn.answer_segments[iteration] = session.segment_state.segments[-1]
            turn.segment_order[id(session.segment_state.segments[-1])] = (iteration, 1)
        # A physical rollover freezes its prefix. Reordering that prefix would
        # shift split_index and replay already delivered answers onto a new card.
        start = session.split_index
        session.segment_state.segments[start:] = sorted(
            session.segment_state.segments[start:],
            key=lambda item: turn.segment_order.get(id(item), (turn.iteration, 2)),
        )
        turn.answer = text

    async def observe(self, kind: str, session_key: str, payload: dict[str, Any]) -> None:
        turn = self.route(session_key, payload)
        if turn is None:
            return
        turn.model = str(payload.get("model") or turn.model)
        turn.api_mode = str(payload.get("api_mode") or turn.api_mode)
        turn.native_tool_prefixes.update(payload.get("_native_prefixes") or [])
        if kind == "stop":
            turn.interrupted = turn.terminal = True
            turn.stop_command = payload.get("invalidation_reason") == "stop_command" or (
                str(payload.get("reason") or "").lstrip("/") == "stop"
            )
            await self.complete(turn)
            return
        if kind == "start":
            turn.iteration = int(payload.get("api_call_count") or turn.iteration)
            await self.ensure_card(turn)
            return
        if kind == "end":
            turn.terminal = True
            turn.failed = bool(payload.get("failed"))
            turn.interrupted = bool(payload.get("interrupted"))
            return
        if kind == "usage":
            request_id = str(payload.get("api_request_id") or "")
            if not request_id or request_id in turn.requests:
                return
            turn.requests.add(request_id)
            usage = payload.get("usage") or {}
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "total_tokens",
            ):
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    turn.usage[key] = turn.usage.get(key, 0) + value
            turn.context = {"used_tokens": usage.get("prompt_tokens"), "max_tokens": payload.get("context_length")}
            self.controller.on_turn_usage(message_id=turn.message_id, usage=turn.usage, api_calls=len(turn.requests))
            iteration = int(payload.get("api_call_count") or turn.iteration)
            if await self.ensure_card(turn):
                reasoning = str(payload.get("_reasoning") or "")
                session = self.controller._sessions.get(turn.message_id)
                segment = turn.reasoning_segments.get(iteration)
                if reasoning and session is not None and session.segment_state and self.controller._cfg.show_reasoning:
                    if segment is not None and segment in session.segment_state.segments:
                        segment.text, segment.dirty = reasoning, True
                    else:
                        self.controller.on_reasoning(
                            message_id=turn.message_id,
                            text=reasoning,
                            api_mode=turn.api_mode,
                        )
                        if session.segment_state.segments:
                            turn.reasoning_segments[iteration] = session.segment_state.segments[-1]
                            turn.segment_order[id(session.segment_state.segments[-1])] = (iteration, 0)
                    if self.controller._cfg.reasoning_mode == "merged":
                        text = (
                            reasoning
                            if self.controller._is_activity_reasoning_api_mode(turn.api_mode)
                            else ("\n\n".join(str(item.text) for item in turn.reasoning_segments.values()))
                        )
                        session.merged_reasoning.replace_activity(text)
                # Raw model content is an ordering hint, never presentation text.
                # Only host transport snapshots may replace the visible answer.
                turn.expected_answers[iteration] = str(payload.get("_answer") or "")
            turn.closed_iterations.add(iteration)
            return
        if kind in {"human_request", "human_resolved"}:
            request_id = str(payload.get("request_id") or "")
            if kind == "human_request":
                turn.pending_inputs.add(request_id)
                if payload.get("kind") == "clarify":
                    self.controller.on_clarify_enter(message_id=turn.message_id)
            else:
                turn.pending_inputs.discard(request_id)
                if payload.get("kind") == "clarify":
                    self.controller.on_clarify_exit(message_id=turn.message_id)
            return
        if turn.terminal or turn.fallback:
            return
        if kind == "reasoning" and payload.get("kind") != "reasoning":
            return
        iteration = int(payload.get("iteration") or turn.iteration)
        if kind == "reasoning" and iteration in turn.closed_iterations:
            return
        if not await self.ensure_card(turn):
            return
        if kind == "reasoning":
            accepted = self.controller.on_reasoning(
                message_id=turn.message_id,
                text=str(payload.get("delta") or ""),
                api_mode=turn.api_mode,
            )
            session = self.controller._sessions.get(turn.message_id)
            if accepted and session and session.segment_state and session.segment_state.segments:
                turn.reasoning_segments[iteration] = session.segment_state.segments[-1]
                turn.segment_order[id(session.segment_state.segments[-1])] = (iteration, 0)
        elif kind in {"tool_start", "tool_end"}:
            args = payload.get("args") or payload.get("tool_input") or {}
            detail = (args.get("command") or args.get("description") or "") if isinstance(args, dict) else ""
            result = payload.get("result")
            if kind == "tool_end" and result is not None:
                detail = result if isinstance(result, str) else str(result)
            await asyncio.to_thread(
                self.controller.on_tool_update,
                message_id=turn.message_id,
                tool_name=str(payload.get("tool_name") or ""),
                status="started" if kind == "tool_start" else "completed",
                detail=str(detail),
            )
            session = self.controller._sessions.get(turn.message_id)
            if session and session.segment_state and session.segment_state.segments:
                segment = session.segment_state.segments[-1]
                if segment.type == SegmentType.TOOL:
                    turn.segment_order.setdefault(id(segment), (turn.iteration, 2))
