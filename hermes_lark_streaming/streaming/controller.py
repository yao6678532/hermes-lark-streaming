"""流式卡片的异步 API 编排 — 创建、刷新、拆卡、完成."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from functools import partial
from typing import TYPE_CHECKING, Any

from ..cardkit.builder import (
    REASONING_ELEMENT_ID,
    REASONING_TEXT_ELEMENT_ID,
    STREAMING_ELEMENT_ID,
    TOOL_PANEL_ELEMENT_ID,
    build_background_card,
    build_complete_card,
    build_cron_card,
    build_streaming_card_v2,
    estimate_cardkit_elements,
)
from ..cardkit.markdown import (
    _downgrade_tables,
    optimize_markdown_style,
)
from ..feishu import (
    CARDKIT_CONTENT_FAILED,
    CARDKIT_ELEMENT_LIMIT,
    CARDKIT_ELEMENT_NOT_FOUND,
    CARDKIT_RATE_LIMITED,
    CARDKIT_STREAMING_CLOSED,
    FeishuAPIError,
)
from .diagnostics import compact_ids, extract_missing_element_id, segment_state_for_log, summarize_actions
from .flush import CARDKIT_MS
from .image import ImageResolver
from .presentation import CardViewSnapshot, ToolPanelSnapshot, project_card_view, project_tool_panel
from .segment_helper import (
    ELEMENT_THRESHOLD,
    FOOTER_RESERVE,
    INTERIM_PREVIEW_ELEMENT_ESTIMATE,
    MERGED_REASONING_ELEMENT_ESTIMATE,
    active_tool_range,
    build_add_interim_preview_action,
    build_add_merged_reasoning_action,
    build_add_segment_action,
    build_add_tool_panel_action,
    build_progress_update_action,
    build_reasoning_finalized_action,
    build_tool_update_action,
    content_tail_element_id,
    estimate_segment_elements,
    estimate_tool_elements,
    find_tool_split_offset,
)
from .segments import Segment, SegmentState, SegmentType
from .session import SessionState
from .text import split_reasoning_text
from .tooluse import ToolUseTracker

if TYPE_CHECKING:
    from ..config import Config
    from ..feishu import FeishuClient
    from .session import CardSession
    from .tooluse import ToolDisplayStep

_logger = logging.getLogger("hermes_lark_streaming")
_ACTIVITY_REASONING_API_MODES = frozenset({"codex_responses", "codex_app_server"})
_NATIVE_REASONING_SOURCE = "native_reasoning"
_INTERIM_COMMENTARY_SOURCE = "interim_commentary"
_STREAM_ELEMENT_RETRY_DELAYS_SEC = (0.2, 0.4, 0.8)


async def _resolve_answer_images(
    segments: list[Segment],
    resolver: ImageResolver,
    *,
    log_prefix: str,
) -> None:
    """解析 answer segment 中的 markdown 图片，并原地更新文本."""
    for seg in segments:
        if seg.type != SegmentType.ANSWER or not seg.text:
            continue
        try:
            seg.text = await resolver.resolve_await(seg.text)
        except Exception:
            _logger.debug("%s image resolve failed: el=%s", log_prefix, seg.el_id, exc_info=True)


class StreamingController:
    """流式卡片专用方法 — 由 StreamCardController 继承."""

    _client: FeishuClient | None
    _cfg: Config
    _ensure_init: Callable[..., Coroutine[Any, Any, None]]
    _cleanup: Callable[[str], None]
    _cleanup_session: Callable[[CardSession], None]
    _conversation_lock: Callable[[CardSession], asyncio.Lock]
    _get_active_session: Callable[[str], CardSession | None]
    _register_latest_card: Callable[[CardSession], bool]
    _remember_finalized_card: Callable[[CardSession, dict[str, Any]], None]
    _wait_for_card_creation: Callable[[CardSession], Coroutine[Any, Any, bool]]

    @staticmethod
    def _is_missing_element_error(error: FeishuAPIError) -> bool:
        return (
            error.code == CARDKIT_ELEMENT_NOT_FOUND
            or error.extract_sub_code() == CARDKIT_ELEMENT_NOT_FOUND
        )

    @staticmethod
    def _segment_owns_stream(
        session: CardSession,
        segment_state: SegmentState,
        segment: Segment,
        segment_type: SegmentType,
        rendered_text: str,
    ) -> bool:
        return (
            session.segment_state is segment_state
            and segment in segment_state.segments[session.split_index:]
            and segment.type == segment_type
            and segment.created
            and segment.dirty
            and segment.text == rendered_text
        )

    def _stream_retry_cancel_reason(
        self,
        session: CardSession,
        *,
        expected_card_id: str,
        expected_generation: int,
        owns_element: Callable[[], bool],
    ) -> str | None:
        if session.card_generation != expected_generation:
            return "generation_changed"
        if session.card_id != expected_card_id:
            return "card_changed"
        if (
            session.state != SessionState.STREAMING
            or session.clarify_pending_split
            or session.flush.completed
        ):
            return "state_changed"
        if session.guard.should_skip("cardkit_stream_retry"):
            return "unavailable"
        if self._get_active_session(session.message_id) is not session:
            return "session_replaced"
        if not owns_element():
            return "ownership_changed"
        return None

    async def _stream_element_with_propagation_retry(
        self,
        session: CardSession,
        *,
        element_id: str,
        content: str,
        source: str,
        just_created: bool,
        owns_element: Callable[[], bool],
        ownership_element_ids: set[str],
    ) -> bool:
        """Stream one newly-created element with a bounded 300313 propagation retry.

        The caller holds the conversation lock.  Backoff deliberately releases it,
        then reacquires and revalidates physical-card and presentation ownership.
        ``False`` means ownership went stale; callers must not run recovery against
        the replacement presentation.
        """
        assert self._client is not None
        assert session.card_id is not None
        expected_card_id = session.card_id
        expected_generation = session.card_generation
        conversation_lock = self._conversation_lock(session)

        for attempt in range(len(_STREAM_ELEMENT_RETRY_DELAYS_SEC) + 1):
            cancel_reason = self._stream_retry_cancel_reason(
                session,
                expected_card_id=expected_card_id,
                expected_generation=expected_generation,
                owns_element=owns_element,
            )
            if cancel_reason:
                _logger.debug(
                    "cardkit_stream_retry_cancelled msg=%s card=%s generation=%d "
                    "element=%s source=%s reason=%s",
                    session.message_id[:12],
                    expected_card_id[:12],
                    expected_generation,
                    element_id,
                    source,
                    cancel_reason,
                )
                return False

            session.sequence += 1
            sequence = session.sequence
            try:
                await self._client.cardkit_stream_element(
                    expected_card_id,
                    element_id,
                    content,
                    sequence=sequence,
                )
                return True
            except FeishuAPIError as error:
                missing_element_id = extract_missing_element_id(error)
                retryable = (
                    just_created
                    and self._is_missing_element_error(error)
                    and (
                        not missing_element_id
                        or missing_element_id in ownership_element_ids
                    )
                )
                if not retryable:
                    raise
                if attempt == len(_STREAM_ELEMENT_RETRY_DELAYS_SEC):
                    _logger.debug(
                        "cardkit_stream_retry_exhausted msg=%s card=%s generation=%d "
                        "element=%s source=%s attempts=%d",
                        session.message_id[:12],
                        expected_card_id[:12],
                        expected_generation,
                        element_id,
                        source,
                        attempt,
                    )
                    raise

                delay = _STREAM_ELEMENT_RETRY_DELAYS_SEC[attempt]
                _logger.debug(
                    "cardkit_stream_retry msg=%s card=%s generation=%d element=%s "
                    "source=%s attempt=%d/%d delay_ms=%d",
                    session.message_id[:12],
                    expected_card_id[:12],
                    expected_generation,
                    element_id,
                    source,
                    attempt + 1,
                    len(_STREAM_ELEMENT_RETRY_DELAYS_SEC),
                    round(delay * 1000),
                )
                if not conversation_lock.locked():
                    raise RuntimeError("CardKit stream retry requires conversation lock") from error
                conversation_lock.release()
                try:
                    await asyncio.sleep(delay)
                finally:
                    await conversation_lock.acquire()

        raise AssertionError("unreachable")

    def _schedule_flush(self, session: CardSession, *, urgent: bool = False) -> None:
        if session.state == SessionState.IDLE or session.state.is_terminal:
            return
        if session.state == SessionState.CLARIFY_PAUSED:
            return
        if session.guard.should_skip("_schedule_flush"):
            return
        expected_generation = session.card_generation
        expected_card_id = session.card_id
        first_visible_generation = expected_generation if urgent else None
        session.flush.schedule_update(
            lambda: self._do_flush(
                session,
                expected_generation=expected_generation,
                expected_card_id=expected_card_id,
                first_visible_generation=first_visible_generation,
            ),
            urgent=urgent,
        )

    def _schedule_visible_flush(self, session: CardSession, *, source: str) -> None:
        """Use the one-shot urgent lane for a physical card's first visible mutation."""
        if not session.first_visible_pending_source:
            session.first_visible_pending_source = source

        generation = session.card_generation
        urgent = (
            session.state == SessionState.STREAMING
            and bool(session.card_id)
            and session.first_visible_urgent_generation != generation
            and session.first_visible_rendered_generation != generation
        )
        if urgent:
            session.first_visible_urgent_generation = generation
            _logger.debug(
                "first_visible_flush msg=%s card=%s source=%s generation=%d mode=urgent",
                session.message_id[:12],
                (session.card_id or "")[:12],
                session.first_visible_pending_source,
                generation,
            )
        self._schedule_flush(session, urgent=urgent)

    @staticmethod
    def _mark_first_visible_rendered(session: CardSession, *, source: str) -> None:
        """Record a successful user-visible mutation on the current physical card."""
        if session.first_visible_rendered_generation == session.card_generation:
            return
        if session.first_visible_urgent_generation != session.card_generation:
            _logger.debug(
                "first_visible_flush msg=%s card=%s source=%s generation=%d mode=inflight",
                session.message_id[:12],
                (session.card_id or "")[:12],
                source,
                session.card_generation,
            )
        session.first_visible_rendered_generation = session.card_generation
        session.first_visible_pending_source = None

    def _pending_visible_source(self, session: CardSession) -> str | None:
        """Derive a visible dirty lane for state accumulated before card readiness."""
        pending_source = session.first_visible_pending_source
        if pending_source and self._has_visible_dirty_source(session, pending_source):
            return pending_source
        session.first_visible_pending_source = None
        if (
            session.interim_preview.dirty
            and not session.interim_preview.final_started
            and session.interim_preview.text.strip()
        ):
            return "commentary"
        segment_state = session.segment_state
        if segment_state is None:
            return None
        for segment in segment_state.segments[session.split_index:]:
            if not segment.dirty or not segment.text.strip():
                continue
            if segment.type == SegmentType.ANSWER:
                return "answer"
            if segment.type == SegmentType.REASONING and self._cfg.show_reasoning:
                return "reasoning"
        if (
            self._cfg.show_tool_use
            and session.tool_panel.dirty
            and session.tool_use.build_display_steps()
        ):
            return "tool"
        return None

    def _has_visible_dirty_source(self, session: CardSession, source: str) -> bool:
        """Check the current presentation state, not merely callback history."""
        if source == "commentary":
            return bool(
                session.interim_preview.dirty
                and not session.interim_preview.final_started
                and session.interim_preview.text.strip()
            )
        if source == "tool":
            return bool(
                self._cfg.show_tool_use
                and session.tool_panel.dirty
                and session.tool_use.build_display_steps()
            )
        segment_state = session.segment_state
        if segment_state is None:
            return False
        segment_type = (
            SegmentType.ANSWER if source == "answer" else SegmentType.REASONING
        )
        if segment_type == SegmentType.REASONING and not self._cfg.show_reasoning:
            return False
        return any(
            segment.type == segment_type
            and segment.dirty
            and bool(segment.text.strip())
            for segment in segment_state.segments[session.split_index:]
        )

    def _schedule_progress_only_flush(self, session: CardSession) -> None:
        """Flush only a paused session's fixed progress element.

        Clarify deliberately pauses the normal segment pipeline.  Heartbeats
        still need a CardKit home, but must never resume answer/reasoning/tool
        flushing or target a card after a clarify handoff.
        """
        expected_card_id = session.card_id
        if not expected_card_id:
            return

        async def flush_progress_only() -> None:
            try:
                async with self._conversation_lock(session):
                    if (
                        self._get_active_session(session.message_id) is not session
                        or session.state != SessionState.CLARIFY_PAUSED
                        or session.card_id != expected_card_id
                        or not session.progress.available
                        or not session.progress.dirty
                        or session.guard.should_skip("_schedule_progress_only_flush")
                    ):
                        return
                    await self._flush_progress(session)
            except Exception:
                _logger.debug("paused CardKit progress flush failed", exc_info=True)

        try:
            asyncio.get_running_loop().create_task(flush_progress_only())
        except RuntimeError:
            _logger.debug("no running loop for paused CardKit progress flush")

    async def _flush_progress(self, session: CardSession) -> None:
        """Update the fixed status element while preserving concurrent events."""
        if self._cfg.progress_mode != "card" or not session.progress.dirty:
            return
        assert self._client is not None
        assert session.card_id is not None
        snapshot = session.progress.snapshot()
        if not snapshot.visible:
            return
        session.sequence += 1
        try:
            await self._client.cardkit_batch_update(
                session.card_id,
                [build_progress_update_action(snapshot)],
                sequence=session.sequence,
            )
        except Exception:
            _logger.debug("CardKit progress stream failed", exc_info=True)
            session.progress.disable()
            return
        session.progress.mark_rendered(snapshot.revision)

    def _record_reasoning(self, session: CardSession, text: str, *, activity: bool) -> None:
        """Preserve chronology while applying the source's presentation semantics."""
        segment_state = session.segment_state
        if segment_state is None:
            return
        segment_state.on_reasoning_delta(text)
        if self._cfg.reasoning_mode == "merged":
            if activity:
                session.merged_reasoning.replace_activity(text)
            else:
                session.merged_reasoning.append_delta(text)

    def _append_reasoning(self, session: CardSession, text: str) -> None:
        """Ingest thinking-tag reasoning, whose callback supplies true deltas."""
        self._record_reasoning(session, text, activity=False)

    @staticmethod
    def _is_activity_reasoning_api_mode(api_mode: str | None) -> bool:
        """Whether this Hermes runtime emits activity-style reasoning updates."""
        return str(api_mode or "").strip().lower() in _ACTIVITY_REASONING_API_MODES

    @classmethod
    def _uses_activity_reasoning_presentation(
        cls,
        *,
        api_mode: str | None,
        source: str | None,
    ) -> bool:
        """Select replace-only activity UI from an explicit runtime/source contract.

        Missing or unknown metadata deliberately falls back to delta append: an
        overly verbose panel is safer than losing provider-supplied reasoning.
        """
        normalized_source = str(source or "").strip().lower()
        return (
            normalized_source == _NATIVE_REASONING_SOURCE
            and cls._is_activity_reasoning_api_mode(api_mode)
        )

    def _record_native_reasoning(
        self, session: CardSession, text: str, *, api_mode: str,
    ) -> None:
        """Route native reasoning by the Hermes transport contract, not model name."""
        self._record_reasoning(
            session,
            text,
            activity=self._uses_activity_reasoning_presentation(
                api_mode=api_mode,
                source=_NATIVE_REASONING_SOURCE,
            ),
        )

    def _pause_merged_reasoning(self, session: CardSession) -> None:
        if self._cfg.reasoning_mode == "merged":
            session.merged_reasoning.pause()

    def _start_final(self, session: CardSession, *, source: str) -> bool:
        """Start the final lane and log the first transition with its caller."""
        preview = session.interim_preview
        previous_revision = preview.revision
        was_created = preview.created
        was_dirty = preview.dirty
        had_text = bool(preview.text)
        started = session.interim_preview.start_final()
        if started:
            _logger.info(
                "preview_final_start msg=%s source=%s previous_revision=%d "
                "was_created=%s was_dirty=%s had_text=%s "
                "flush_in_progress=%s pending_flush=%s",
                session.message_id[:12],
                source,
                previous_revision,
                was_created,
                was_dirty,
                had_text,
                session.flush.flush_in_progress,
                session.flush.has_pending_timer,
            )
        return started

    @staticmethod
    def _active_card_has_answer(session: CardSession) -> bool:
        """Return whether the current physical card already has answer text."""
        segment_state = session.segment_state
        if segment_state is None:
            return False
        return any(
            seg.type == SegmentType.ANSWER and seg.text.strip()
            for seg in segment_state.segments[session.split_index:]
        )

    def _append_answer_segment(self, session: CardSession, text: str) -> bool:
        """Append answer text and mark the tool panel only on first-card answer."""
        segment_state = session.segment_state
        if segment_state is None or not text:
            return False

        had_answer = self._active_card_has_answer(session)
        segment_state.on_answer_delta(text)
        has_answer = self._active_card_has_answer(session)
        if not had_answer and has_answer:
            session.tool_panel.note_answer_started()
        return True

    @staticmethod
    def _consume_merged_reasoning_segments(session: CardSession) -> None:
        """Mark chronology segments consumed only after the fixed UI is current."""
        if not session.merged_reasoning.created or session.merged_reasoning.dirty:
            return
        if session.segment_state is None:
            return
        for seg in session.segment_state.segments:
            if seg.type == SegmentType.REASONING:
                seg.created = True
                seg.dirty = False

    async def _flush_merged_reasoning(self, session: CardSession) -> bool:
        """Create/update the fixed reasoning lane without affecting other segments."""
        assert self._client is not None
        assert session.card_id is not None
        state = session.merged_reasoning
        if not state.text:
            return True

        just_created = False
        if not state.created:
            session.sequence += 1
            try:
                await self._client.cardkit_batch_update(
                    session.card_id,
                    [
                        build_add_merged_reasoning_action(
                            target_element_id=content_tail_element_id(
                                agent_status_created=session.agent_status_created
                            )
                        )
                    ],
                    sequence=session.sequence,
                )
            except FeishuAPIError as error:
                _logger.debug("CardKit merged reasoning create failed: %s", error, exc_info=True)
                self._handle_flush_error(error)
                return True
            except Exception:
                _logger.debug("CardKit merged reasoning create failed", exc_info=True)
                return True
            state.created = True
            just_created = True
            session.element_count += MERGED_REASONING_ELEMENT_ESTIMATE

        if state.dirty:
            rendered_text = state.text
            content = optimize_markdown_style(rendered_text) or " "
            try:
                streamed = await self._stream_element_with_propagation_retry(
                    session,
                    element_id=REASONING_TEXT_ELEMENT_ID,
                    content=content,
                    source="merged_reasoning",
                    just_created=just_created,
                    owns_element=lambda: (
                        session.merged_reasoning is state
                        and state.created
                        and state.dirty
                        and state.text == rendered_text
                    ),
                    ownership_element_ids={
                        REASONING_ELEMENT_ID,
                        REASONING_TEXT_ELEMENT_ID,
                    },
                )
                if not streamed:
                    return False
            except FeishuAPIError as error:
                missing_el_id = extract_missing_element_id(error)
                if self._is_missing_element_error(error) and (
                    not missing_el_id
                    or missing_el_id in {REASONING_ELEMENT_ID, REASONING_TEXT_ELEMENT_ID}
                ):
                    state.created = False
                    session.element_count = max(
                        0,
                        session.element_count - MERGED_REASONING_ELEMENT_ESTIMATE,
                    )
                _logger.debug("CardKit merged reasoning stream failed: %s", error, exc_info=True)
                self._handle_flush_error(error)
                return True
            except Exception:
                _logger.debug("CardKit merged reasoning stream failed", exc_info=True)
                return True
            if rendered_text.strip():
                self._mark_first_visible_rendered(session, source="reasoning")
            if state.text == rendered_text:
                state.dirty = False

        self._consume_merged_reasoning_segments(session)
        return True

    async def _flush_interim_preview(self, session: CardSession) -> bool:
        """Create/update the replace-only commentary preview lane fail-open."""
        assert self._client is not None
        assert session.card_id is not None
        state = session.interim_preview

        _logger.debug(
            "commentary_flush_start msg=%s revision=%d created=%s dirty=%s final_started=%s",
            session.message_id[:12],
            state.revision,
            state.created,
            state.dirty,
            state.final_started,
        )

        just_created = False
        if not state.created and state.text and not state.final_started:
            session.sequence += 1
            try:
                await self._client.cardkit_batch_update(
                    session.card_id,
                    [
                        build_add_interim_preview_action(
                            text_size=self._cfg.body_text_size,
                            target_element_id=content_tail_element_id(
                                agent_status_created=session.agent_status_created
                            ),
                        )
                    ],
                    sequence=session.sequence,
                )
            except FeishuAPIError as error:
                _logger.debug("CardKit interim preview create failed: %s", error, exc_info=True)
                self._handle_flush_error(error)
                return True
            except Exception:
                _logger.debug("CardKit interim preview create failed", exc_info=True)
                return True
            state.created = True
            just_created = True
            session.element_count += INTERIM_PREVIEW_ELEMENT_ESTIMATE

        if not state.created or not state.dirty:
            return True

        rendered_text = state.text
        rendered_revision = state.revision
        content = _downgrade_tables(optimize_markdown_style(rendered_text)) or " "
        try:
            streamed = await self._stream_element_with_propagation_retry(
                session,
                element_id=STREAMING_ELEMENT_ID,
                content=content,
                source="commentary",
                just_created=just_created,
                owns_element=lambda: (
                    session.interim_preview is state
                    and state.created
                    and state.dirty
                    and state.revision == rendered_revision
                    and state.text == rendered_text
                ),
                ownership_element_ids={STREAMING_ELEMENT_ID},
            )
            if not streamed:
                return False
        except FeishuAPIError as error:
            missing_el_id = extract_missing_element_id(error)
            if self._is_missing_element_error(error) and (
                not missing_el_id or missing_el_id == STREAMING_ELEMENT_ID
            ):
                state.created = False
                state.dirty = bool(state.text) and not state.final_started
                session.element_count = max(
                    0,
                    session.element_count - INTERIM_PREVIEW_ELEMENT_ESTIMATE,
                )
            _logger.debug("CardKit interim preview stream failed: %s", error, exc_info=True)
            self._handle_flush_error(error)
            return True
        except Exception:
            _logger.debug("CardKit interim preview stream failed", exc_info=True)
            return True

        if rendered_text.strip():
            self._mark_first_visible_rendered(session, source="commentary")
        state.mark_rendered(rendered_revision)
        _logger.debug(
            "commentary_rendered msg=%s revision=%d",
            session.message_id[:12],
            rendered_revision,
        )
        return True

    def _on_thinking_segment(
        self,
        session: CardSession,
        text: str,
        *,
        api_mode: str = "",
        source: str = "",
    ) -> bool:
        segment_state = session.segment_state
        if segment_state is None:
            return False

        normalized_source = str(source or "").strip().lower()
        if normalized_source == _INTERIM_COMMENTARY_SOURCE:
            if not text:
                _logger.debug(
                    "stream lane=commentary ignored=empty msg=%s len=0 final_started=%s",
                    session.message_id[:12],
                    session.interim_preview.final_started,
                )
                return False
            if session.interim_preview.final_started:
                _logger.debug(
                    "stream lane=commentary ignored=final_started msg=%s len=%d",
                    session.message_id[:12],
                    len(text),
                )
                return False
            self._pause_merged_reasoning(session)
            if not session.interim_preview.replace(text):
                return False
            preview = session.interim_preview
            _logger.debug(
                "commentary_received msg=%s revision=%d created=%s dirty=%s "
                "final_started=%s flush_in_progress=%s pending_flush=%s",
                session.message_id[:12],
                preview.revision,
                preview.created,
                preview.dirty,
                preview.final_started,
                session.flush.flush_in_progress,
                session.flush.has_pending_timer,
            )
            if text.strip():
                self._schedule_visible_flush(session, source="commentary")
            else:
                self._schedule_flush(session)
            return True

        activity = self._uses_activity_reasoning_presentation(
            api_mode=api_mode,
            source=normalized_source,
        )
        reasoning: str | None
        answer: str | None
        split = split_reasoning_text(text)
        reasoning = split.get("reasoning_text")
        answer = split.get("answer_text")

        visible_reasoning = bool(reasoning and reasoning.strip() and self._cfg.show_reasoning)
        visible_answer = bool(answer and answer.strip())
        if reasoning and self._cfg.show_reasoning:
            self._record_reasoning(session, reasoning, activity=activity)
        if answer:
            _logger.debug(
                "answer_lane_start msg=%s source=thinking_answer visible_len=%d",
                session.message_id[:12],
                len(answer),
            )
            self._pause_merged_reasoning(session)
            if answer.strip():
                self._start_final(session, source="thinking_answer")
            self._append_answer_segment(session, answer)
        if not (reasoning and self._cfg.show_reasoning) and not answer:
            return False
        if visible_reasoning or visible_answer:
            self._schedule_visible_flush(
                session,
                source="reasoning" if visible_reasoning else "answer",
            )
        else:
            self._schedule_flush(session)
        return True

    async def _do_create_card(self, session: CardSession) -> None:
        """创建只有 loading 的流式占位卡片."""
        if session.state != SessionState.IDLE:
            return
        session.state = SessionState.CREATING
        if session.segment_state is None:
            session.segment_state = SegmentState()

        try:
            await self._ensure_init()
            assert self._client is not None

            reply_to_message_id = session.anchor_id or session.message_id
            progress_snapshot = (
                session.progress.snapshot()
                if self._cfg.progress_mode == "card"
                else None
            )
            card = build_streaming_card_v2(
                show_tool_use=False,
                show_reasoning=False,
                show_streaming_element=False,
                header_enabled=self._cfg.header_enabled,
                text_size=self._cfg.body_text_size,
                width_mode=self._cfg.width_mode,
                progress_snapshot=progress_snapshot,
            )
            card_id = await self._client.cardkit_create(card)
            try:
                card_msg_id = await self._client.reply_card_by_id(
                    reply_to_message_id,
                    card_id,
                )
            except FeishuAPIError as error:
                if error.code != CARDKIT_CONTENT_FAILED:
                    raise
                card_id = await self._client.cardkit_create(card)
                try:
                    card_msg_id = await self._client.reply_card_by_id(
                        reply_to_message_id,
                        card_id,
                    )
                except FeishuAPIError:
                    card_msg_id = await self._client.send_card_to_chat(
                        chat_id=session.chat_id,
                        card={"type": "card", "data": {"card_id": card_id}},
                    )
            session.set_card(
                card_id=card_id,
                card_msg_id=card_msg_id,
                preserve_pending_visible=True,
            )
            async with self._conversation_lock(session):
                self._register_latest_card(session)
            session.element_count = 1
            if progress_snapshot is not None and progress_snapshot.visible:
                session.progress.mark_rendered(progress_snapshot.revision)
            session.flush.set_throttle(CARDKIT_MS)

            if session.image_resolver is None and self._client:
                session.image_resolver = ImageResolver(
                    client=self._client,
                    on_image_resolved=lambda: self._schedule_flush(session),
                )

            session.flush.set_card_message_ready(True)
            if session.state == SessionState.CREATING:
                session.state = SessionState.STREAMING
            if (
                (session.segment_state and session.segment_state.has_dirty)
                or session.progress.dirty
                or session.interim_preview.dirty
            ):
                visible_source = self._pending_visible_source(session)
                if visible_source:
                    self._schedule_visible_flush(session, source=visible_source)
                else:
                    self._schedule_flush(session)
            _logger.info(
                "CardKit card created: msg=%s card_id=%s",
                session.message_id[:12],
                (session.card_id or "")[:12],
            )
        except FeishuAPIError:
            _logger.info("CardKit create failed, yielding to gateway", exc_info=True)
            if hasattr(self, "_mark_text_fallback_needed"):
                self._mark_text_fallback_needed(session)
            session.mark_failed()
        except Exception:
            _logger.exception("_do_create_card failed")
            session.mark_failed()

    async def _do_flush(
        self,
        session: CardSession,
        *,
        expected_generation: int | None = None,
        expected_card_id: str | None = None,
        first_visible_generation: int | None = None,
    ) -> None:
        if (
            expected_generation is not None
            and (
                session.card_generation != expected_generation
                or session.card_id != expected_card_id
            )
        ):
            return
        try:
            async with self._conversation_lock(session):
                if (
                    expected_generation is not None
                    and (
                        session.card_generation != expected_generation
                        or session.card_id != expected_card_id
                    )
                ):
                    return
                await self._do_flush_inner(session)
        finally:
            if (
                first_visible_generation is not None
                and session.card_generation == first_visible_generation
                and session.first_visible_rendered_generation != first_visible_generation
            ):
                session.first_visible_urgent_generation = -1

    async def _do_flush_inner(self, session: CardSession) -> None:
        """幂等 flush：按 segment 顺序处理结构性变更，超阈值时拆卡."""
        if session.state.is_terminal or session.state == SessionState.CLARIFY_PAUSED or not session.card_id:
            return
        segment_state = session.segment_state
        if segment_state is None:
            return

        assert self._client is not None
        segments = segment_state.segments
        all_steps = session.tool_use.build_display_steps()
        merged_mode = self._cfg.reasoning_mode == "merged"
        show_tool_use = self._cfg.show_tool_use
        show_tool_detail = self._cfg.show_tool_detail
        tool_detail_mode = self._cfg.tool_detail_mode
        pending_preview = (
            INTERIM_PREVIEW_ELEMENT_ESTIMATE
            if session.interim_preview.text
            and not session.interim_preview.created
            and not session.interim_preview.final_started
            else 0
        )

        await self._flush_progress(session)

        # This lane is presentation state. Numbered reasoning segments remain
        # intact for chronology, splitting, and diagnostics.
        if merged_mode and not await self._flush_merged_reasoning(session):
            return

        # ── 步骤 1: batch_update — chronology 保留，TOOL 由固定 presentation lane 消费 ──
        actions: list[dict[str, Any]] = []
        new_el_ids: set[str] = set()
        new_el_estimates: dict[str, int] = {}
        tool_panel_segments: list[Segment] = []
        tool_panel_snapshot: tuple[int, int, list[Segment], list[ToolDisplayStep]] | None = None
        new_el_total = 0
        tool_panel_seen = False

        for i, seg in enumerate(segments):
            if i < session.split_index:
                continue
            if merged_mode and seg.type == SegmentType.REASONING:
                continue

            if seg.type == SegmentType.TOOL:
                if not show_tool_use:
                    seg.created = True
                    seg.dirty = False
                    continue
                if tool_panel_seen:
                    seg.created = True
                    seg.dirty = False
                    continue
                tool_panel_seen = True
                if not all_steps:
                    seg.created = True
                    seg.dirty = False
                    continue
                # The fixed panel is a bounded presentation of *all* logical
                # tool history, not a physical copy of this card's chronology
                # slice.  Remove its old estimate first to learn the budget
                # available to the new projection.
                tool_budget = (
                    ELEMENT_THRESHOLD
                    - FOOTER_RESERVE
                    - pending_preview
                    - new_el_total
                    - (session.element_count - session.tool_panel.element_estimate)
                )
                presentation = project_tool_panel(
                    all_steps,
                    show_tool_detail=show_tool_detail,
                    tool_detail_mode=tool_detail_mode,
                    element_budget=max(0, tool_budget),
                )
                panel_estimate = presentation.estimated_elements
                if (
                    not session.tool_panel.created
                    and not session.tool_panel.dirty
                    and seg.created
                    and seg.element_estimate > 0
                ):
                    # Compatibility with a session restored from the previous
                    # per-segment renderer: infer the fixed panel's already
                    # rendered estimate from its chronology segment once.
                    session.tool_panel.created = True
                    session.tool_panel.element_estimate = seg.element_estimate
                panel_needs_update = (
                    not session.tool_panel.created
                    or session.tool_panel.dirty
                    or any(
                        tool_seg.dirty
                        for tool_seg in segments[session.split_index:]
                        if tool_seg.type == SegmentType.TOOL
                    )
                )
                source_statuses = tuple(str(step.get("status", "")) for step in all_steps)
                if (
                    panel_needs_update
                    and session.interim_preview.final_started
                    and session.tool_panel.created
                    and not session.tool_panel.final_answer_refresh_pending
                    and not session.tool_panel.needs_final_refresh(source_statuses)
                ):
                    panel_needs_update = False
                if panel_needs_update:
                    current_estimate = session.tool_panel.element_estimate
                    delta = panel_estimate - current_estimate
                    _logger.info(
                        "tool_snapshot: msg=%s total_steps=%d rendered_steps=%d configured_mode=%s "
                        "actual_mode=%s estimated_elements=%d budget=%d degraded=%s windowed=%s "
                        "success_results_visible=%s",
                        session.message_id[:12],
                        presentation.total_steps,
                        presentation.rendered_steps,
                        presentation.configured_mode,
                        presentation.mode,
                        panel_estimate,
                        tool_budget,
                        presentation.degraded,
                        presentation.windowed,
                        presentation.success_results_visible,
                    )
                    if (
                        panel_estimate > tool_budget
                        and not session.split_disabled
                    ):
                        next_presentation = project_tool_panel(
                            all_steps,
                            show_tool_detail=show_tool_detail,
                            tool_detail_mode=tool_detail_mode,
                            element_budget=ELEMENT_THRESHOLD - FOOTER_RESERVE - 1,
                        )
                        if not next_presentation.steps:
                            _logger.warning(
                                "CardKit split skipped: no meaningful tool snapshot msg=%s "
                                "estimate=%d threshold=%d",
                                session.message_id[:12],
                                panel_estimate,
                                ELEMENT_THRESHOLD,
                            )
                            session.split_disabled = True
                            return
                        previous_split_index = session.split_index
                        previous_card_id = session.card_id
                        split_ok = await self._do_split_card(
                            session,
                            i,
                            actions,
                            new_el_ids,
                            new_el_estimates,
                            tool_panel_segments,
                            next_tool_snapshot=(
                                next_presentation,
                                [
                                    tool_seg
                                    for tool_seg in segments[i:]
                                    if tool_seg.type == SegmentType.TOOL
                                ],
                            ),
                            show_tool_detail=show_tool_detail,
                            tool_detail_mode=tool_detail_mode,
                        )
                        if not split_ok:
                            return
                        if session.split_index != previous_split_index or session.card_id != previous_card_id:
                            actions = []
                            new_el_ids = set()
                            new_el_estimates = {}
                            tool_panel_segments = []
                            tool_panel_snapshot = None
                            new_el_total = 0
                            tool_panel_seen = False
                            return await self._do_flush_inner(session)
                        actions = []
                        new_el_ids = set()
                        new_el_estimates = {}
                        tool_panel_segments = []
                        tool_panel_snapshot = None
                        new_el_total = 0
                    if session.tool_panel.created:
                        actions.append(
                            build_tool_update_action(
                                steps=list(presentation.steps),
                                total_steps=presentation.total_steps,
                                total_failed_count=presentation.total_failed_count,
                                expanded=self._tool_panel_expanded(session),
                                show_tool_detail=presentation.show_tool_detail,
                                tool_detail_mode=presentation.tool_detail_mode,
                            )
                        )
                    else:
                        actions.append(
                            build_add_tool_panel_action(
                                list(presentation.steps),
                                total_steps=presentation.total_steps,
                                total_failed_count=presentation.total_failed_count,
                                expanded=self._tool_panel_expanded(session),
                                show_tool_detail=presentation.show_tool_detail,
                                tool_detail_mode=presentation.tool_detail_mode,
                                target_element_id=content_tail_element_id(
                                    agent_status_created=session.agent_status_created
                                ),
                            )
                        )
                    tool_panel_segments = [
                        tool_seg
                        for tool_seg in segments[session.split_index:]
                        if tool_seg.type == SegmentType.TOOL
                    ]
                    tool_panel_snapshot = (
                        session.tool_panel.revision,
                        panel_estimate,
                        tool_panel_segments,
                        list(presentation.steps),
                    )
                    new_el_total += delta
                continue

            if not seg.created:
                estimated = estimate_segment_elements(
                    seg,
                    all_steps,
                    show_tool_detail=show_tool_detail,
                    tool_detail_mode=tool_detail_mode,
                )
                if (
                    session.element_count
                    + new_el_total
                    + pending_preview
                    + estimated
                    + FOOTER_RESERVE
                    > ELEMENT_THRESHOLD
                    and session.element_count + new_el_total > 1
                    and not session.split_disabled
                ):
                    split_ok = await self._do_split_card(
                        session,
                        i,
                        actions,
                        new_el_ids,
                        new_el_estimates,
                        tool_panel_segments,
                        show_tool_detail=show_tool_detail,
                        tool_detail_mode=tool_detail_mode,
                    )
                    if not split_ok:
                        return
                    actions = []
                    new_el_ids = set()
                    new_el_estimates = {}
                    tool_panel_segments = []
                    tool_panel_snapshot = None
                    new_el_total = 0
                new_el_ids.add(seg.el_id)
                new_el_estimates[seg.el_id] = estimated
                new_el_total += estimated
                actions.append(
                    build_add_segment_action(
                        seg,
                        all_steps,
                        text_size=self._cfg.body_text_size,
                        show_tool_detail=show_tool_detail,
                        tool_detail_mode=tool_detail_mode,
                        target_element_id=content_tail_element_id(
                            agent_status_created=session.agent_status_created
                        ),
                    )
                )
            elif seg.type == SegmentType.REASONING and seg.elapsed_ms > 0 and not seg.reasoning_finalized:
                _logger.info(
                    "CardKit reasoning finalized: msg=%s el=%s elapsed=%.0fms seq=%d",
                    session.message_id[:12], seg.el_id, seg.elapsed_ms, session.sequence + 1,
                )
                actions.append(build_reasoning_finalized_action(seg))

        if actions and not await self._do_batch_update(
            session,
            segments,
            actions,
            new_el_ids,
            new_el_estimates,
            tool_panel_segments,
            tool_panel_snapshot=tool_panel_snapshot,
        ):
            return

        # Keep the transient commentary lane after structural reasoning/tool
        # elements and before normal answer/segment text streaming.
        if not await self._flush_interim_preview(session):
            return

        # ── 步骤 2: stream_element 刷脏文本 ──
        for seg in segments[session.split_index:]:
            if not seg.created or not seg.dirty:
                continue
            try:
                if seg.type == SegmentType.REASONING:
                    if merged_mode:
                        continue
                    rendered_text = seg.text
                    content = optimize_markdown_style(rendered_text) or " "
                    _logger.info(
                        "CardKit stream element: msg=%s seq=%d type=reasoning len=%d",
                        session.message_id[:12],
                        session.sequence + 1,
                        len(content),
                    )
                    streamed = await self._stream_element_with_propagation_retry(
                        session,
                        element_id=seg.text_el_id,
                        content=content,
                        source="reasoning",
                        just_created=seg.el_id in new_el_ids,
                        owns_element=partial(
                            self._segment_owns_stream,
                            session,
                            segment_state,
                            seg,
                            SegmentType.REASONING,
                            rendered_text,
                        ),
                        ownership_element_ids={seg.el_id, seg.text_el_id},
                    )
                    if not streamed:
                        return
                    if rendered_text.strip():
                        self._mark_first_visible_rendered(session, source="reasoning")
                    if seg.text == rendered_text:
                        seg.dirty = False
                elif seg.type == SegmentType.ANSWER:
                    rendered_text = seg.text
                    content = rendered_text
                    if session.image_resolver:
                        content = session.image_resolver.resolve_images(content)
                    content = _downgrade_tables(optimize_markdown_style(content)) or " "
                    _logger.info(
                        "CardKit stream element: msg=%s seq=%d type=answer len=%d",
                        session.message_id[:12],
                        session.sequence + 1,
                        len(content),
                    )
                    streamed = await self._stream_element_with_propagation_retry(
                        session,
                        element_id=seg.el_id,
                        content=content,
                        source="answer",
                        just_created=seg.el_id in new_el_ids,
                        owns_element=partial(
                            self._segment_owns_stream,
                            session,
                            segment_state,
                            seg,
                            SegmentType.ANSWER,
                            rendered_text,
                        ),
                        ownership_element_ids={seg.el_id},
                    )
                    if not streamed:
                        return
                    if rendered_text.strip():
                        self._mark_first_visible_rendered(session, source="answer")
                    if seg.text == rendered_text:
                        seg.dirty = False
            except FeishuAPIError as e:
                missing_element_id = extract_missing_element_id(e)
                owned_ids = {seg.el_id, seg.text_el_id} - {""}
                if self._is_missing_element_error(e) and (
                    not missing_element_id or missing_element_id in owned_ids
                ):
                    seg.created = False
                    seg.dirty = True
                    session.element_count = max(
                        0,
                        session.element_count - seg.element_estimate,
                    )
                    _logger.info(
                        "CardKit recovered stale segment %s -> will re-add on next flush",
                        seg.el_id,
                    )
                _logger.debug("CardKit stream element failed: %s el=%s", e, seg.el_id, exc_info=True)
                self._handle_flush_error(e)
            except Exception as e:
                _logger.debug("CardKit stream element failed: %s el=%s", e, seg.el_id, exc_info=True)

    async def _do_batch_update(
        self,
        session: CardSession,
        segments: list[Segment],
        actions: list[dict[str, Any]],
        new_el_ids: set[str],
        new_el_estimates: dict[str, int],
        updated_tool_segs: list[Segment],
    *,
    tool_panel_snapshot: tuple[int, int, list[Segment], list[ToolDisplayStep]] | None = None,
) -> bool:
        """执行 batch_update 并处理快照/标记。返回 False 表示失败."""
        assert self._client is not None
        assert session.card_id is not None
        session.sequence += 1
        _logger.info(
            "CardKit batch update: msg=%s card=%s seq=%d actions=%d split=%d elements=%d",
            session.message_id[:12],
            session.card_id[:12],
            session.sequence,
            len(actions),
            session.split_index,
            session.element_count,
        )
        pre_flush_reasoning_elapsed = {
            seg.el_id: seg.elapsed_ms for seg in segments if seg.type == SegmentType.REASONING
        }
        pre_flush_tool_steps = session.tool_use.build_display_steps()
        pre_flush_tool_offsets = {
            seg.el_id: seg.tool_end_offset for seg in updated_tool_segs
        }
        visible_tool_panel = bool(tool_panel_snapshot and tool_panel_snapshot[3])
        try:
            await self._client.cardkit_batch_update(
                session.card_id,
                actions,
                sequence=session.sequence,
            )
            for seg in segments:
                if seg.el_id in new_el_ids:
                    seg.created = True
                    estimate = new_el_estimates.get(seg.el_id, 0)
                    seg.element_estimate = estimate
                    session.element_count += estimate
            for seg in segments:
                if seg.type == SegmentType.REASONING and pre_flush_reasoning_elapsed.get(seg.el_id, 0) > 0:
                    seg.reasoning_finalized = True
            if new_el_ids:
                for seg in segments:
                    if seg.el_id in new_el_ids or not seg.created:
                        continue
                    if self._cfg.reasoning_mode == "merged" and seg.type == SegmentType.REASONING:
                        continue
                    if seg.type in (SegmentType.REASONING, SegmentType.ANSWER) and seg.text:
                        seg.dirty = True
            current_tool_steps = session.tool_use.build_display_steps()
            current_statuses = tuple(str(step.get("status", "")) for step in current_tool_steps)
            for seg in updated_tool_segs:
                if seg.el_id in new_el_estimates:
                    estimate = new_el_estimates[seg.el_id]
                    session.element_count += estimate - seg.element_estimate
                    seg.element_estimate = estimate
                if (
                    seg.created
                    and pre_flush_tool_steps == current_tool_steps
                    and pre_flush_tool_offsets.get(seg.el_id) == seg.tool_end_offset
                ):
                    seg.dirty = False
            if tool_panel_snapshot is not None:
                revision, estimate, panel_segments, _rendered_steps = tool_panel_snapshot
                session.element_count += estimate - session.tool_panel.element_estimate
                current = session.tool_panel.mark_rendered(
                    revision,
                    estimate,
                    statuses=current_statuses,
                )
                offsets_changed = any(
                    pre_flush_tool_offsets.get(panel_seg.el_id) != panel_seg.tool_end_offset
                    for panel_seg in panel_segments
                )
                if pre_flush_tool_steps != current_tool_steps or offsets_changed:
                    session.tool_panel.dirty = True
                for panel_seg in panel_segments:
                    panel_seg.created = True
                    panel_seg.dirty = session.tool_panel.dirty or not current or offsets_changed
            if visible_tool_panel:
                self._mark_first_visible_rendered(session, source="tool")
        except FeishuAPIError as e:
            missing_el_id = extract_missing_element_id(e)
            action_summary = summarize_actions(actions)
            _logger.warning(
                "CardKit batch update failed: %s card=%s seq=%d split=%d elements=%d "
                "missing=%s missing_state=%s new=[%s] tool_updates=[%s] %s",
                e,
                session.card_id[:12],
                session.sequence,
                session.split_index,
                session.element_count,
                missing_el_id or "-",
                segment_state_for_log(segments, missing_el_id),
                compact_ids(new_el_ids),
                compact_ids([seg.el_id for seg in updated_tool_segs]),
                action_summary,
                exc_info=True,
            )
            # 缺失元素（300313）时回滚 stale segment：本地 created=True 但卡片上不存在，
            # 下一轮 flush 会用 add_elements 重建该元素，避免反复 partial_update 死循环。
            if missing_el_id:
                panel_state_reset = False
                if missing_el_id == TOOL_PANEL_ELEMENT_ID:
                    old_estimate = session.tool_panel.element_estimate
                    session.tool_panel.reset_render_state(dirty=True)
                    session.element_count = max(0, session.element_count - old_estimate)
                    panel_state_reset = True
                    for panel_seg in segments[session.split_index:]:
                        if panel_seg.type == SegmentType.TOOL:
                            panel_seg.created = False
                            panel_seg.dirty = True
                    _logger.info(
                        "CardKit recovered stale unified tool panel -> will re-add on next flush"
                    )
                for seg in segments[session.split_index:]:
                    if seg.el_id == missing_el_id and seg.created:
                        seg.created = False
                        seg.dirty = True
                        if seg.type == SegmentType.TOOL and not panel_state_reset:
                            old_estimate = session.tool_panel.element_estimate
                            session.tool_panel.reset_render_state(dirty=True)
                            session.element_count = max(0, session.element_count - old_estimate)
                            panel_state_reset = True
                        # 同步扣减元素计数：该 segment 当初 add 成功时已累加进 element_count，
                        # 回滚为未创建后下一轮会重新 add 并再次累加，这里先扣除避免重复计数。
                        if seg.type != SegmentType.TOOL or not panel_state_reset:
                            session.element_count -= seg.element_estimate
                            if session.element_count < 0:
                                session.element_count = 0
                        _logger.info(
                            "CardKit recovered stale segment %s -> will re-add on next flush",
                            seg.el_id,
                        )
                        break
            self._handle_flush_error(e)
            return False
        return True

    @staticmethod
    def _tool_panel_expanded(session: CardSession) -> bool:
        """Derive live expansion from structured tool/answer state."""
        if session.tool_use.has_running:
            return True
        segment_state = session.segment_state
        has_answer = bool(
            segment_state
            and any(
                seg.type == SegmentType.ANSWER and seg.text.strip()
                for seg in segment_state.segments[session.split_index:]
            )
        )
        return not has_answer

    async def _maybe_rollover_unified_tool_panel(
        self,
        *,
        session: CardSession,
        split_index: int,
        all_steps: list[ToolDisplayStep],
        actions: list[dict[str, Any]],
        new_el_ids: set[str],
        new_el_estimates: dict[str, int],
        tool_panel_segments: list[Segment],
        pending_delta: int = 0,
        show_tool_detail: bool = True,
        tool_detail_mode: str = "full",
    ) -> str | None:
        """Split a unified panel at a tool step boundary when it outgrows the card."""
        segment_state = session.segment_state
        if segment_state is None:
            return None
        seg = segment_state.segments[split_index]
        if seg.type != SegmentType.TOOL:
            return None
        active_range = active_tool_range(segment_state.segments, session.split_index, all_steps)
        if active_range is None:
            return None
        active_start, _ = active_range
        base_count = (
            session.element_count
            + pending_delta
            - session.tool_panel.element_estimate
        )
        split_offset = find_tool_split_offset(
            base_count=base_count,
            seg=seg,
            all_steps=all_steps,
            show_tool_detail=show_tool_detail,
            tool_detail_mode=tool_detail_mode,
        )
        split_target_index = split_index + 1
        if split_offset is None:
            # A one-step segment cannot be split internally.  Prefer a later
            # chronology boundary when the current card can still retain a
            # fitting prefix of the unified panel.
            tool_segments = [
                (index, candidate)
                for index, candidate in enumerate(segment_state.segments[session.split_index:], session.split_index)
                if candidate.type == SegmentType.TOOL
            ]
            fitting_boundary: tuple[int, int] | None = None
            for candidate_index, candidate in tool_segments[1:]:
                candidate_end = candidate.tool_offset
                if candidate_end <= active_start:
                    continue
                candidate_estimate = estimate_tool_elements(
                    active_start,
                    candidate_end,
                    all_steps,
                    show_tool_detail=show_tool_detail,
                    tool_detail_mode=tool_detail_mode,
                )
                if base_count + candidate_estimate + FOOTER_RESERVE <= ELEMENT_THRESHOLD:
                    fitting_boundary = (candidate_index, candidate_end)
            if fitting_boundary is None:
                return None
            split_target_index, split_offset = fitting_boundary
        else:
            split_target_index = split_index + 1
        panel_steps = all_steps[active_start:split_offset]
        if not panel_steps:
            return None
        panel_presentation = project_tool_panel(
            panel_steps,
            show_tool_detail=show_tool_detail,
            tool_detail_mode=tool_detail_mode,
            element_budget=max(0, ELEMENT_THRESHOLD - base_count - FOOTER_RESERVE),
        )
        panel_estimate = panel_presentation.estimated_elements
        panel_action = (
            build_tool_update_action(
                steps=list(panel_presentation.steps),
                total_steps=panel_presentation.total_steps,
                total_failed_count=panel_presentation.total_failed_count,
                expanded=self._tool_panel_expanded(session),
                show_tool_detail=panel_presentation.show_tool_detail,
                tool_detail_mode=panel_presentation.tool_detail_mode,
            )
            if session.tool_panel.created
            else build_add_tool_panel_action(
                list(panel_presentation.steps),
                total_steps=panel_presentation.total_steps,
                total_failed_count=panel_presentation.total_failed_count,
                expanded=self._tool_panel_expanded(session),
                show_tool_detail=panel_presentation.show_tool_detail,
                tool_detail_mode=panel_presentation.tool_detail_mode,
                target_element_id=content_tail_element_id(
                    agent_status_created=session.agent_status_created
                ),
            )
        )
        actions.append(panel_action)
        tool_panel_segments = [
            candidate
            for index, candidate in enumerate(segment_state.segments[session.split_index:], session.split_index)
            if candidate.type == SegmentType.TOOL and index < split_target_index
        ]
        tool_panel_snapshot = (
            session.tool_panel.revision,
            panel_estimate,
            tool_panel_segments,
            list(panel_presentation.steps),
        )
        if split_target_index == split_index + 1:
            segment_state.split_tool_segment(split_index, split_offset)
        split_ok = await self._do_split_card(
            session,
            split_target_index,
            actions,
            new_el_ids,
            new_el_estimates,
            tool_panel_segments,
            tool_panel_snapshot=tool_panel_snapshot,
            show_tool_detail=show_tool_detail,
            tool_detail_mode=tool_detail_mode,
        )
        return "split" if split_ok else "failed"

    async def _seal_current_card(
        self,
        session: CardSession,
        seal_segments: list[Segment],
        *,
        card_id: str | None = None,
        sequence: int | None = None,
    ) -> None:
        """封印指定卡（默认 session.card_id）：close_streaming + 全量重建。失败仅记录日志。

        card_id 用于 session 已切到新卡、仍需封印旧卡的场景（clarify 切卡）；
        sequence 传入旧卡续用的递增序列（CardKit 要求单调递增，不能用新卡的计数）。
        """
        assert self._client is not None
        old_card_id = card_id or session.card_id
        if not old_card_id:
            return
        if session.image_resolver:
            await _resolve_answer_images(
                seal_segments,
                session.image_resolver,
                log_prefix="CardKit seal",
            )
        all_steps = session.tool_use.build_display_steps()
        merged_mode = self._cfg.reasoning_mode == "merged"
        merged_text = None
        merged_elapsed_ms = 0.0
        if merged_mode:
            merged_text = "".join(
                segment.text for segment in seal_segments if segment.type == SegmentType.REASONING
            )
            merged_elapsed_ms = sum(
                segment.elapsed_ms
                for segment in seal_segments
                if segment.type == SegmentType.REASONING
            )
        non_tool_card = build_complete_card(
            segments=seal_segments,
            all_tool_steps=all_steps,
            footer_fields=[],
            footer_show_label=False,
            footer_enabled=False,
            panel_expanded=self._cfg.panel_expanded,
            header_enabled=False,
            body_text_size=self._cfg.body_text_size,
            add_empty_answer_fallback=False,
            show_tool_use=False,
            width_mode=self._cfg.width_mode,
            merged_reasoning_text=merged_text,
            merged_reasoning_elapsed_ms=merged_elapsed_ms,
        )
        tool_snapshot = project_tool_panel(
            all_steps,
            show_tool_detail=self._cfg.show_tool_detail,
            tool_detail_mode=self._cfg.tool_detail_mode,
            element_budget=max(
                0,
                ELEMENT_THRESHOLD - estimate_cardkit_elements(non_tool_card),
            ),
        )
        seal_card = build_complete_card(
            segments=seal_segments,
            all_tool_steps=all_steps,
            footer_fields=[],
            footer_show_label=False,
            footer_enabled=False,
            panel_expanded=self._cfg.panel_expanded,
            header_enabled=False,
            body_text_size=self._cfg.body_text_size,
            add_empty_answer_fallback=False,
            show_tool_use=self._cfg.show_tool_use,
            show_tool_detail=tool_snapshot.show_tool_detail,
            tool_detail_mode=tool_snapshot.tool_detail_mode,
            tool_panel_steps=list(tool_snapshot.steps),
            tool_total_steps=tool_snapshot.total_steps,
            tool_total_failed_count=tool_snapshot.total_failed_count,
            width_mode=self._cfg.width_mode,
            merged_reasoning_text=merged_text,
            merged_reasoning_elapsed_ms=merged_elapsed_ms,
        )
        try:
            seq = session.sequence if sequence is None else sequence
            seq += 1
            await self._client.cardkit_close_streaming(old_card_id, sequence=seq)
            seq += 1
            await self._client.cardkit_update(old_card_id, seal_card, sequence=seq)
        except Exception:
            _logger.warning(
                "CardKit seal failed for old card %s, continuing",
                old_card_id[:12],
                exc_info=True,
            )

    async def _create_streaming_card(self, session: CardSession) -> tuple[str, str] | None:
        """创建空白流式卡并挂到 anchor，返回 (card_id, msg_id)。失败返回 None。

        不修改 session.card_id —— 调用方负责 set_card。
        """
        assert self._client is not None
        try:
            progress_snapshot = (
                session.progress.snapshot()
                if self._cfg.progress_mode == "card"
                else None
            )
            card = build_streaming_card_v2(
                show_tool_use=False,
                show_reasoning=False,
                show_streaming_element=False,
                header_enabled=self._cfg.header_enabled,
                text_size=self._cfg.body_text_size,
                width_mode=self._cfg.width_mode,
                progress_snapshot=progress_snapshot,
                agent_status=session.agent_status,
            )
            new_card_id = await self._client.cardkit_create(card)
            new_msg_id = await self._client.reply_card_by_id(
                session.anchor_id or session.message_id, new_card_id,
            )
        except Exception:
            _logger.warning(
                "CardKit create streaming card failed for msg=%s",
                session.message_id[:12],
                exc_info=True,
            )
            return None
        return new_card_id, new_msg_id

    async def _do_split_card(
        self,
        session: CardSession,
        split_idx: int,
        actions: list[dict[str, Any]],
        new_el_ids: set[str],
        new_el_estimates: dict[str, int],
        updated_tool_segs: list[Segment],
        *,
        tool_panel_snapshot: tuple[int, int, list[Segment], list[ToolDisplayStep]] | None = None,
        next_tool_snapshot: tuple[ToolPanelSnapshot, list[Segment]] | None = None,
        show_tool_detail: bool = True,
        tool_detail_mode: str = "full",
    ) -> bool:
        """拆卡：先 flush pending actions，封旧卡，创建新卡。返回 False 表示失败需中断 flush."""
        assert self._client is not None
        old_card_id = session.card_id
        assert old_card_id is not None
        segment_state = session.segment_state
        assert segment_state is not None
        segments = segment_state.segments
        all_steps = session.tool_use.build_display_steps()
        seal_start_idx = session.split_index

        if actions and not await self._do_batch_update(
            session,
            segments,
            actions,
            new_el_ids,
            new_el_estimates,
            updated_tool_segs,
            tool_panel_snapshot=tool_panel_snapshot,
        ):
            return False

        seal_segments = [s for s in segments[seal_start_idx:split_idx] if s.created]

        merged_mode = self._cfg.reasoning_mode == "merged"
        seal_merged_text = None
        seal_merged_elapsed_ms = 0.0
        if merged_mode:
            seal_merged_text = "".join(
                seg.text for seg in seal_segments if seg.type == SegmentType.REASONING
            )
            seal_merged_elapsed_ms = sum(
                seg.elapsed_ms for seg in seal_segments if seg.type == SegmentType.REASONING
            )

        seal_non_tool_card = build_complete_card(
            segments=seal_segments,
            all_tool_steps=all_steps,
            footer_fields=[],
            footer_show_label=False,
            footer_enabled=False,
            panel_expanded=self._cfg.panel_expanded,
            header_enabled=False,
            body_text_size=self._cfg.body_text_size,
            add_empty_answer_fallback=False,
            show_tool_use=False,
            width_mode=self._cfg.width_mode,
            merged_reasoning_text=seal_merged_text,
            merged_reasoning_elapsed_ms=seal_merged_elapsed_ms,
        )
        seal_tool_snapshot = project_tool_panel(
            all_steps,
            show_tool_detail=show_tool_detail,
            tool_detail_mode=tool_detail_mode,
            element_budget=max(0, ELEMENT_THRESHOLD - estimate_cardkit_elements(seal_non_tool_card)),
        )
        seal_card = build_complete_card(
            segments=seal_segments,
            all_tool_steps=all_steps,
            footer_fields=[],
            footer_show_label=False,
            footer_enabled=False,
            panel_expanded=self._cfg.panel_expanded,
            header_enabled=False,
            body_text_size=self._cfg.body_text_size,
            add_empty_answer_fallback=False,
            show_tool_use=self._cfg.show_tool_use,
            show_tool_detail=seal_tool_snapshot.show_tool_detail,
            tool_detail_mode=seal_tool_snapshot.tool_detail_mode,
            tool_panel_steps=list(seal_tool_snapshot.steps),
            tool_total_steps=seal_tool_snapshot.total_steps,
            tool_total_failed_count=seal_tool_snapshot.total_failed_count,
            width_mode=self._cfg.width_mode,
            merged_reasoning_text=seal_merged_text,
            merged_reasoning_elapsed_ms=seal_merged_elapsed_ms,
        )

        try:
            progress_snapshot = (
                session.progress.snapshot()
                if self._cfg.progress_mode == "card"
                else None
            )
            card = build_streaming_card_v2(
                show_tool_use=False,
                show_reasoning=False,
                show_streaming_element=False,
                header_enabled=self._cfg.header_enabled,
                text_size=self._cfg.body_text_size,
                width_mode=self._cfg.width_mode,
                progress_snapshot=progress_snapshot,
                agent_status=session.agent_status,
            )
            new_card_id = await self._client.cardkit_create(card)
            new_msg_id = await self._client.reply_card_by_id(session.anchor_id or session.message_id, new_card_id)
        except Exception:
            _logger.warning(
                "CardKit split fallback: create next card failed, continue on current card",
                exc_info=True,
            )
            # 拆卡失败时降级为继续写当前卡，并禁用后续拆卡重试以避免反复卡在同一边界。
            session.split_disabled = True
            return True

        try:
            session.sequence += 1
            await self._client.cardkit_close_streaming(old_card_id, sequence=session.sequence)
            session.sequence += 1
            await self._client.cardkit_update(old_card_id, seal_card, sequence=session.sequence)
        except Exception:
            _logger.warning(
                "CardKit seal failed for old card %s, continuing",
                old_card_id[:12],
                exc_info=True,
            )

        session.set_card(card_id=new_card_id, card_msg_id=new_msg_id)
        session.agent_status_created = bool(session.agent_status)
        session.element_count = 1 + (2 if session.agent_status_created else 0)
        session.sequence = 1
        self._register_latest_card(session)
        if progress_snapshot is not None and progress_snapshot.visible:
            session.progress.mark_rendered(progress_snapshot.revision)
        session.split_disabled = False
        session.split_index = split_idx
        for seg in segments[split_idx:]:
            seg.created = False
        session.tool_panel.reset_render_state(
            dirty=any(seg.type == SegmentType.TOOL for seg in segments[split_idx:])
        )
        session.interim_preview.reset_render_state()
        if merged_mode:
            # The new card replays the accumulated lane. Older sealed cards keep
            # only the merged reasoning that belongs to their chronology slice.
            session.merged_reasoning.reset_render_state()
            if not await self._flush_merged_reasoning(session):
                return False
        if next_tool_snapshot is not None:
            next_presentation, next_panel_segments = next_tool_snapshot
            if not next_presentation.steps:
                _logger.warning(
                    "CardKit split created no follow-up payload: msg=%s", session.message_id[:12],
                )
                session.split_disabled = True
                return True
            next_actions = [
                build_add_tool_panel_action(
                    list(next_presentation.steps),
                    total_steps=next_presentation.total_steps,
                    total_failed_count=next_presentation.total_failed_count,
                    expanded=self._tool_panel_expanded(session),
                    show_tool_detail=next_presentation.show_tool_detail,
                    tool_detail_mode=next_presentation.tool_detail_mode,
                    target_element_id=content_tail_element_id(
                        agent_status_created=session.agent_status_created
                    ),
                )
            ]
            if not await self._do_batch_update(
                session,
                segments,
                next_actions,
                set(),
                {},
                next_panel_segments,
                tool_panel_snapshot=(
                    session.tool_panel.revision,
                    next_presentation.estimated_elements,
                    next_panel_segments,
                    list(next_presentation.steps),
                ),
            ):
                session.split_disabled = True
                return False
        _logger.info(
            "CardKit split: msg=%s old_card=%s sealed=%d split_idx=%d new_card=%s",
            session.message_id[:12],
            old_card_id[:12],
            len(seal_segments),
            split_idx,
            new_card_id[:12],
        )
        return True

    async def _do_clarify_split(self, session: CardSession) -> bool:
        """clarify 工具结束后切卡：建新卡 + 封旧卡。

        返回 False 表示未切卡（无卡可封，或建卡失败降级继续写旧卡）。
        """
        await self._wait_for_card_creation(session)
        if not session.has_card or session.state == SessionState.FAILED:
            _logger.info(
                "clarify_split: no card to seal, msg=%s state=%s",
                session.message_id[:12], session.state,
            )
            return False

        # 先禁拆卡再等 flush：否则进行中的 flush 可能先拆卡，随后被本流程封印成空白卡。
        session.split_disabled = True
        try:
            await session.flush.wait_for_flush()
            try:
                await session.flush.flush_now(lambda: self._do_flush(session))
            except Exception:
                _logger.debug("clarify_split: final flush failed", exc_info=True)

            # final flush 自己会获取 conversation lock，所以必须在锁外完成。真正的物理卡
            # ownership handoff（create → switch → registry → seal/reset）则和 Agent Status
            # 共用同一把 conversation lock。这样新卡构建时读取到的 status snapshot 与
            # handoff 时的 authoritative session.agent_status 必然一致。
            async with self._conversation_lock(session):
                # 先建新卡后封旧卡（与拆卡一致）：建卡失败时旧卡未 close，可降级继续流式。
                new_card = await self._create_streaming_card(session)
                if new_card is None:
                    _logger.warning(
                        "clarify_split: create new card failed, continuing on current card, msg=%s",
                        session.message_id[:12],
                    )
                    return False

                # 先切到新卡再封旧卡：任意时刻被取消（超时兜底）时，session 指向的都是
                # 未 close 的卡，后续 flush 不会写到已关闭的旧卡。
                # seal 必须显式传旧 card_id + 旧 sequence（session 已切新卡，
                # 且 CardKit sequence 要求单调递增，不能用新卡的计数）。
                old_card_id = session.card_id
                old_seq = session.sequence
                new_card_id, new_msg_id = new_card
                session.set_card(card_id=new_card_id, card_msg_id=new_msg_id)
                session.sequence = 1  # 新卡从 1 重新计数
                session.agent_status_created = bool(session.agent_status)
                progress_snapshot = (
                    session.progress.snapshot()
                    if self._cfg.progress_mode == "card"
                    else None
                )
                self._register_latest_card(session)
                if progress_snapshot is not None and progress_snapshot.visible:
                    session.progress.mark_rendered(progress_snapshot.revision)

                await self._seal_current_card(
                    session,
                    session.active_segments(),
                    card_id=old_card_id,
                    sequence=old_seq,
                )

                # 重置内容，新卡承载 clarify 后的输出
                session.segment_state = SegmentState()
                session.split_index = 0
                session.element_count = 1 + (2 if session.agent_status_created else 0)
                session.tool_use = ToolUseTracker()
                session.tool_panel.reset_render_state(dirty=False)
                session.interim_preview.reset_render_state()
                session.merged_reasoning.reset_render_state()

            _logger.info(
                "clarify_split: sealed old + new card msg=%s card=%s",
                session.message_id[:12], new_card_id[:12],
            )
            return True
        finally:
            session.split_disabled = False  # 取消/异常/失败均恢复拆卡能力

    def _handle_flush_error(self, e: FeishuAPIError) -> None:
        if e.code == CARDKIT_RATE_LIMITED:
            return
        if e.code == CARDKIT_STREAMING_CLOSED:
            return
        if e.code == CARDKIT_CONTENT_FAILED:
            sub_code = e.extract_sub_code()
            if sub_code == CARDKIT_ELEMENT_LIMIT:
                _logger.warning("CardKit card element limit exceeded")

    async def _do_complete_card(self, session: CardSession) -> bool:
        """完成流式卡片：close streaming + 全量重建卡片（保持 segments 顺序）."""
        session.progress.clear()
        self._start_final(session, source="terminal")
        await session.flush.wait_for_flush()
        session.flush.mark_completed()
        async with self._conversation_lock(session):
            try:
                return await self._do_complete_card_inner(session)
            finally:
                self._cleanup_session(session)

    async def _do_complete_card_inner(self, session: CardSession) -> bool:
        if session.guard.should_skip("_do_complete_card"):
            return False

        segment_state = session.segment_state
        is_error = session.state == SessionState.FAILED
        is_aborted = session.state == SessionState.ABORTED
        all_tool_steps = session.tool_use.build_display_steps()
        show_tool_detail = self._cfg.show_tool_detail
        tool_detail_mode = self._cfg.tool_detail_mode

        if segment_state is not None:
            segment_state.finalize_segments(len(all_tool_steps))
        if self._cfg.reasoning_mode == "merged":
            session.merged_reasoning.finalize()

        # Earlier rollover slices were already sealed into prior cards.  The
        # terminal card must render only the active slice or it duplicates
        # content from those cards.
        logical_segments = session.active_segments() if segment_state is not None else []
        final_view: CardViewSnapshot = project_card_view(
            segments=logical_segments,
            all_tool_steps=all_tool_steps,
            merged_reasoning=session.merged_reasoning,
            interim_preview=session.interim_preview,
            reasoning_mode=self._cfg.reasoning_mode,
        )
        terminal_non_tool_card = build_complete_card(
            segments=logical_segments,
            all_tool_steps=all_tool_steps,
            footer_data=session.footer,
            is_error=is_error,
            is_aborted=is_aborted,
            footer_fields=self._cfg.footer_fields,
            footer_show_label=self._cfg.footer_show_label,
            footer_enabled=self._cfg.footer_enabled,
            footer_text_size=self._cfg.footer_text_size,
            panel_expanded=self._cfg.panel_expanded,
            header_enabled=self._cfg.header_enabled,
            body_text_size=self._cfg.body_text_size,
            add_empty_answer_fallback=False,
            show_tool_use=False,
            width_mode=self._cfg.width_mode,
            merged_reasoning_text=(
                final_view.reasoning_text if self._cfg.reasoning_mode == "merged" else None
            ),
            merged_reasoning_elapsed_ms=final_view.reasoning_elapsed_ms,
            agent_status=session.agent_status,
        )
        terminal_non_tool_estimate = estimate_cardkit_elements(terminal_non_tool_card)
        final_tool_snapshot = project_tool_panel(
            all_tool_steps,
            show_tool_detail=show_tool_detail,
            tool_detail_mode=tool_detail_mode,
            element_budget=max(0, ELEMENT_THRESHOLD - terminal_non_tool_estimate),
        )

        if session.image_resolver:
            await _resolve_answer_images(
                logical_segments,
                session.image_resolver,
                log_prefix="CardKit",
            )

        card = build_complete_card(
            segments=logical_segments,
            all_tool_steps=all_tool_steps,
            footer_data=session.footer,
            is_error=is_error,
            is_aborted=is_aborted,
            footer_fields=self._cfg.footer_fields,
            footer_show_label=self._cfg.footer_show_label,
            footer_enabled=self._cfg.footer_enabled,
            footer_text_size=self._cfg.footer_text_size,
            panel_expanded=self._cfg.panel_expanded,
            header_enabled=self._cfg.header_enabled,
            body_text_size=self._cfg.body_text_size,
            add_empty_answer_fallback=False,
            show_tool_use=self._cfg.show_tool_use,
            show_tool_detail=final_tool_snapshot.show_tool_detail,
            tool_detail_mode=final_tool_snapshot.tool_detail_mode,
            tool_panel_steps=list(final_tool_snapshot.steps),
            tool_total_steps=final_tool_snapshot.total_steps,
            tool_total_failed_count=final_tool_snapshot.total_failed_count,
            width_mode=self._cfg.width_mode,
            merged_reasoning_text=(
                final_view.reasoning_text
                if self._cfg.reasoning_mode == "merged"
                else None
            ),
            merged_reasoning_elapsed_ms=final_view.reasoning_elapsed_ms,
            agent_status=session.agent_status,
        )

        streaming_closed = False
        for attempt in range(3):
            try:
                assert self._client is not None
                if session.card_id:
                    if not streaming_closed:
                        session.sequence += 1
                        await self._client.cardkit_close_streaming(
                            session.card_id,
                            sequence=session.sequence,
                        )
                        streaming_closed = True
                    session.sequence += 1
                    await self._client.cardkit_update(
                        session.card_id,
                        card,
                        sequence=session.sequence,
                    )
                session.state = SessionState.COMPLETED
                self._remember_finalized_card(session, card)
                return True
            except FeishuAPIError as e:
                _logger.warning(
                    "CardKit complete attempt %d failed: code=%s msg=%s card_id=%s seq=%d",
                    attempt,
                    e.code,
                    e,
                    session.card_id,
                    session.sequence,
                    exc_info=True,
                )
                if session.guard.terminate("_do_complete_card", e):
                    return False
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                continue
            except Exception as e:
                _logger.warning(
                    "CardKit complete attempt %d failed: %s: %s card_id=%s seq=%d",
                    attempt,
                    type(e).__name__,
                    e,
                    session.card_id,
                    session.sequence,
                    exc_info=True,
                )
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                continue

        _logger.error(
            "CardKit complete failed after 3 attempts: card_id=%s seq=%d",
            session.card_id,
            session.sequence,
        )
        session.mark_failed()
        return False

    async def _do_cron_deliver(
        self, chat_id: str, content: str, *, task_name: str = "", run_time: str = ""
    ) -> None:
        await self._ensure_init()
        assert self._client is not None
        card = build_cron_card(content, task_name=task_name, run_time=run_time)
        await self._client.send_card_to_chat(chat_id, card)

    async def _do_background_deliver(
        self,
        chat_id: str,
        preview: str,
        content: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> None:
        await self._ensure_init()
        assert self._client is not None
        card = build_background_card(preview, content)
        await self._client.send_card_to_chat(
            chat_id,
            card,
            reply_to_message_id=reply_to_message_id,
        )
