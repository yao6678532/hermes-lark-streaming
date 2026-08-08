"""流式卡片的异步 API 编排 — 创建、刷新、拆卡、完成."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from ..cardkit.builder import (
    REASONING_ELEMENT_ID,
    REASONING_TEXT_ELEMENT_ID,
    TOOL_PANEL_ELEMENT_ID,
    build_background_card,
    build_complete_card,
    build_cron_card,
    build_streaming_card_v2,
)
from ..cardkit.markdown import (
    _downgrade_tables,
    optimize_markdown_style,
)
from ..feishu import (
    CARDKIT_CONTENT_FAILED,
    CARDKIT_ELEMENT_LIMIT,
    CARDKIT_RATE_LIMITED,
    CARDKIT_STREAMING_CLOSED,
    FeishuAPIError,
)
from .diagnostics import compact_ids, extract_missing_element_id, segment_state_for_log, summarize_actions
from .flush import CARDKIT_MS
from .image import ImageResolver
from .segment_helper import (
    ELEMENT_THRESHOLD,
    FOOTER_RESERVE,
    MERGED_REASONING_ELEMENT_ESTIMATE,
    active_tool_range,
    build_add_merged_reasoning_action,
    build_add_segment_action,
    build_add_tool_panel_action,
    build_progress_update_action,
    build_reasoning_finalized_action,
    build_tool_update_action,
    estimate_segment_elements,
    estimate_tool_elements,
    find_tool_split_offset,
    tool_segment_end,
)
from .segments import Segment, SegmentState, SegmentType
from .session import SessionState
from .text import split_reasoning_text

if TYPE_CHECKING:
    from ..config import Config
    from ..feishu import FeishuClient
    from .session import CardSession
    from .tooluse import ToolDisplayStep

_logger = logging.getLogger("hermes_lark_streaming")
_ACTIVITY_REASONING_API_MODES = frozenset({"codex_responses", "codex_app_server"})
_NATIVE_REASONING_SOURCE = "native_reasoning"
_INTERIM_COMMENTARY_SOURCE = "interim_commentary"


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
    _flush_deferred_background_reviews: Callable[[CardSession], None]

    def _schedule_flush(self, session: CardSession) -> None:
        if session.state == SessionState.IDLE or session.state.is_terminal:
            return
        if session.guard.should_skip("_schedule_flush"):
            return
        session.flush.schedule_update(lambda: self._do_flush(session))

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

    async def _flush_merged_reasoning(self, session: CardSession) -> None:
        """Create/update the fixed reasoning lane without affecting other segments."""
        assert self._client is not None
        assert session.card_id is not None
        state = session.merged_reasoning
        if not state.text:
            return

        if not state.created:
            session.sequence += 1
            try:
                await self._client.cardkit_batch_update(
                    session.card_id,
                    [
                        build_add_merged_reasoning_action()
                    ],
                    sequence=session.sequence,
                )
            except FeishuAPIError as error:
                _logger.debug("CardKit merged reasoning create failed: %s", error, exc_info=True)
                self._handle_flush_error(error)
                return
            except Exception:
                _logger.debug("CardKit merged reasoning create failed", exc_info=True)
                return
            state.created = True
            session.element_count += MERGED_REASONING_ELEMENT_ESTIMATE

        if state.dirty:
            rendered_text = state.text
            content = optimize_markdown_style(rendered_text) or " "
            session.sequence += 1
            try:
                await self._client.cardkit_stream_element(
                    session.card_id,
                    REASONING_TEXT_ELEMENT_ID,
                    content,
                    sequence=session.sequence,
                )
            except FeishuAPIError as error:
                missing_el_id = extract_missing_element_id(error)
                if missing_el_id in {REASONING_ELEMENT_ID, REASONING_TEXT_ELEMENT_ID}:
                    state.created = False
                    session.element_count = max(
                        0,
                        session.element_count - MERGED_REASONING_ELEMENT_ESTIMATE,
                    )
                _logger.debug("CardKit merged reasoning stream failed: %s", error, exc_info=True)
                self._handle_flush_error(error)
                return
            except Exception:
                _logger.debug("CardKit merged reasoning stream failed", exc_info=True)
                return
            if state.text == rendered_text:
                state.dirty = False

        self._consume_merged_reasoning_segments(session)

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
            # Hermes has already classified this as a complete, user-visible
            # assistant message.  Keep commentary in body chronology; only
            # reasoning_callback data is eligible for merged reasoning UI.
            if not text:
                return False
            self._pause_merged_reasoning(session)
            self._append_answer_segment(session, text)
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

        if reasoning and self._cfg.show_reasoning:
            self._record_reasoning(session, reasoning, activity=activity)
        if answer:
            self._pause_merged_reasoning(session)
            self._append_answer_segment(session, answer)
        if not (reasoning and self._cfg.show_reasoning) and not answer:
            return False
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
            session.set_card(card_id=card_id, card_msg_id=card_msg_id)
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
            ):
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

    async def _do_flush(self, session: CardSession) -> None:
        """幂等 flush：按 segment 顺序处理结构性变更，超阈值时拆卡."""
        if session.state.is_terminal or not session.card_id:
            return
        segment_state = session.segment_state
        if segment_state is None:
            return

        assert self._client is not None
        segments = segment_state.segments
        all_steps = session.tool_use.build_display_steps()
        merged_mode = self._cfg.reasoning_mode == "merged"

        await self._flush_progress(session)

        if merged_mode:
            # This lane is presentation state. Numbered reasoning segments remain
            # intact for chronology, splitting, and diagnostics.
            await self._flush_merged_reasoning(session)

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
                if not self._cfg.show_tool_use:
                    seg.created = True
                    seg.dirty = False
                    continue
                if tool_panel_seen:
                    seg.created = True
                    seg.dirty = False
                    continue
                tool_panel_seen = True
                tool_range = active_tool_range(segments, session.split_index, all_steps)
                if tool_range is None:
                    seg.created = True
                    seg.dirty = False
                    continue
                start, end = tool_range
                steps = all_steps[start:end]
                panel_estimate = estimate_tool_elements(start, end, all_steps)
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
                if panel_needs_update:
                    current_estimate = session.tool_panel.element_estimate
                    delta = panel_estimate - current_estimate
                    if (
                        session.element_count + new_el_total + delta + FOOTER_RESERVE > ELEMENT_THRESHOLD
                        and not session.split_disabled
                    ):
                        previous_split_index = session.split_index
                        previous_card_id = session.card_id
                        rollover = await self._maybe_rollover_unified_tool_panel(
                            session=session,
                            split_index=i,
                            all_steps=all_steps,
                            actions=actions,
                            new_el_ids=new_el_ids,
                            new_el_estimates=new_el_estimates,
                            tool_panel_segments=tool_panel_segments,
                            pending_delta=new_el_total,
                        )
                        if rollover == "failed":
                            return
                        if rollover == "split":
                            if session.split_index != previous_split_index or session.card_id != previous_card_id:
                                actions = []
                                new_el_ids = set()
                                new_el_estimates = {}
                                tool_panel_segments = []
                                tool_panel_snapshot = None
                                new_el_total = 0
                                tool_panel_seen = False
                                return await self._do_flush(session)
                            # New-card creation can fail.  The existing card is
                            # intentionally kept alive with split disabled; the
                            # tool panel is then rendered on it in a second batch.
                            actions = []
                            new_el_ids = set()
                            new_el_estimates = {}
                            tool_panel_segments = []
                            tool_panel_snapshot = None
                            new_el_total = 0
                            return await self._do_flush(session)
                    if (
                        session.element_count + new_el_total + delta + FOOTER_RESERVE > ELEMENT_THRESHOLD
                        and session.element_count + new_el_total > 1
                        and not session.split_disabled
                    ):
                        previous_split_index = session.split_index
                        previous_card_id = session.card_id
                        split_ok = await self._do_split_card(
                            session,
                            i,
                            actions,
                            new_el_ids,
                            new_el_estimates,
                            tool_panel_segments,
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
                            return await self._do_flush(session)
                        actions = []
                        new_el_ids = set()
                        new_el_estimates = {}
                        tool_panel_segments = []
                        tool_panel_snapshot = None
                        new_el_total = 0
                    if session.tool_panel.created:
                        actions.append(
                            build_tool_update_action(
                                steps=steps,
                                expanded=self._tool_panel_expanded(session),
                            )
                        )
                    else:
                        actions.append(
                            build_add_tool_panel_action(
                                steps,
                                expanded=self._tool_panel_expanded(session),
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
                        list(steps),
                    )
                    new_el_total += delta
                continue

            if not seg.created:
                estimated = estimate_segment_elements(seg, all_steps)
                if (
                    session.element_count + new_el_total + estimated + FOOTER_RESERVE > ELEMENT_THRESHOLD
                    and session.element_count + new_el_total > 1
                    and not session.split_disabled
                ):
                    split_ok = await self._do_split_card(
                        session, i, actions, new_el_ids, new_el_estimates, tool_panel_segments,
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
                    build_add_segment_action(seg, all_steps, text_size=self._cfg.body_text_size)
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

        # ── 步骤 2: stream_element 刷脏文本 ──
        for seg in segments[session.split_index:]:
            if not seg.created or not seg.dirty:
                continue
            try:
                if seg.type == SegmentType.REASONING:
                    if merged_mode:
                        continue
                    content = optimize_markdown_style(seg.text) or " "
                    session.sequence += 1
                    _logger.info(
                        "CardKit stream element: msg=%s seq=%d type=reasoning len=%d",
                        session.message_id[:12],
                        session.sequence,
                        len(content),
                    )
                    await self._client.cardkit_stream_element(
                        session.card_id,
                        seg.text_el_id,
                        content,
                        sequence=session.sequence,
                    )
                    seg.dirty = False
                elif seg.type == SegmentType.ANSWER:
                    content = seg.text
                    if session.image_resolver:
                        content = session.image_resolver.resolve_images(content)
                    content = _downgrade_tables(optimize_markdown_style(content)) or " "
                    session.sequence += 1
                    _logger.info(
                        "CardKit stream element: msg=%s seq=%d type=answer len=%d",
                        session.message_id[:12],
                        session.sequence,
                        len(content),
                    )
                    await self._client.cardkit_stream_element(
                        session.card_id,
                        seg.el_id,
                        content,
                        sequence=session.sequence,
                    )
                    seg.dirty = False
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
        pre_flush_tool_offsets = {
            seg.el_id: seg.tool_end_offset for seg in updated_tool_segs
        }
        pre_flush_tool_steps = session.tool_use.build_display_steps()
        pre_flush_tool_slices = {
            seg.el_id: pre_flush_tool_steps[seg.tool_offset:tool_segment_end(seg, pre_flush_tool_steps)]
            for seg in updated_tool_segs
        }
        pre_flush_tool_panel_offsets = {
            seg.el_id: (seg.tool_offset, seg.tool_end_offset)
            for seg in (tool_panel_snapshot[2] if tool_panel_snapshot else [])
        }
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
            for seg in updated_tool_segs:
                offset_ok = pre_flush_tool_offsets.get(seg.el_id, -1) == seg.tool_end_offset
                current_tool_slice = current_tool_steps[
                    seg.tool_offset:tool_segment_end(seg, current_tool_steps)
                ]
                tool_slice_ok = pre_flush_tool_slices.get(seg.el_id) == current_tool_slice
                if seg.el_id in new_el_estimates:
                    estimate = new_el_estimates[seg.el_id]
                    session.element_count += estimate - seg.element_estimate
                    seg.element_estimate = estimate
                if seg.created and offset_ok and tool_slice_ok:
                    seg.dirty = False
            if tool_panel_snapshot is not None:
                revision, estimate, panel_segments, rendered_steps = tool_panel_snapshot
                session.element_count += estimate - session.tool_panel.element_estimate
                current_range = active_tool_range(segments, session.split_index, current_tool_steps)
                current_steps = (
                    current_tool_steps[current_range[0]:current_range[1]]
                    if current_range is not None
                    else []
                )
                current = session.tool_panel.mark_rendered(revision, estimate)
                if current_steps != rendered_steps:
                    session.tool_panel.dirty = True
                for panel_seg in panel_segments:
                    panel_seg.created = True
                    offsets_changed = pre_flush_tool_panel_offsets.get(panel_seg.el_id) != (
                        panel_seg.tool_offset,
                        panel_seg.tool_end_offset,
                    )
                    panel_seg.dirty = session.tool_panel.dirty or not current or offsets_changed
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
        panel_estimate = estimate_tool_elements(active_start, split_offset, all_steps)
        panel_action = (
            build_tool_update_action(
                steps=panel_steps,
                expanded=self._tool_panel_expanded(session),
            )
            if session.tool_panel.created
            else build_add_tool_panel_action(
                panel_steps,
                expanded=self._tool_panel_expanded(session),
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
            list(panel_steps),
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
        )
        return "split" if split_ok else "failed"

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
        if session.image_resolver:
            await _resolve_answer_images(
                seal_segments,
                session.image_resolver,
                log_prefix="CardKit seal",
            )

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

        seal_card = build_complete_card(
            segments=seal_segments,
            all_tool_steps=all_steps,
            footer_fields=[],
            footer_show_label=False,
            footer_enabled=False,
            panel_expanded=self._cfg.panel_expanded,
            header_enabled=False,
            body_text_size=self._cfg.body_text_size,
            show_tool_use=self._cfg.show_tool_use,
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
        session.element_count = 1
        if progress_snapshot is not None and progress_snapshot.visible:
            session.progress.mark_rendered(progress_snapshot.revision)
        session.sequence = 1
        session.split_disabled = False
        session.split_index = split_idx
        for seg in segments[split_idx:]:
            seg.created = False
        session.tool_panel.reset_render_state(
            dirty=any(seg.type == SegmentType.TOOL for seg in segments[split_idx:])
        )
        if merged_mode:
            # The new card replays the accumulated lane. Older sealed cards keep
            # only the merged reasoning that belongs to their chronology slice.
            session.merged_reasoning.reset_render_state()
            await self._flush_merged_reasoning(session)
        _logger.info(
            "CardKit split: msg=%s old_card=%s sealed=%d split_idx=%d new_card=%s",
            session.message_id[:12],
            old_card_id[:12],
            len(seal_segments),
            split_idx,
            new_card_id[:12],
        )
        return True

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
        try:
            return await self._do_complete_card_inner(session)
        finally:
            self._flush_deferred_background_reviews(session)
            self._cleanup_session(session)

    async def _do_complete_card_inner(self, session: CardSession) -> bool:
        if session.guard.should_skip("_do_complete_card"):
            return False

        session.progress.clear()
        await session.flush.wait_for_flush()
        session.flush.mark_completed()

        segment_state = session.segment_state
        is_error = session.state == SessionState.FAILED
        is_aborted = session.state == SessionState.ABORTED
        all_tool_steps = session.tool_use.build_display_steps()

        if segment_state is not None:
            segment_state.finalize_segments(len(all_tool_steps))
        if self._cfg.reasoning_mode == "merged":
            session.merged_reasoning.finalize()

        active_segments = session.active_segments()

        if session.image_resolver:
            await _resolve_answer_images(
                active_segments,
                session.image_resolver,
                log_prefix="CardKit",
            )

        card = build_complete_card(
            segments=active_segments,
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
            show_tool_use=self._cfg.show_tool_use,
            width_mode=self._cfg.width_mode,
            merged_reasoning_text=(
                session.merged_reasoning.text
                if self._cfg.reasoning_mode == "merged"
                else None
            ),
            merged_reasoning_elapsed_ms=session.merged_reasoning.elapsed_ms,
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
