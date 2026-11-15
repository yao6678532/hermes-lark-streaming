"""Native Feishu adapter; reuse the local CardKit design and controller."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from contextvars import ContextVar
from typing import Any

from gateway.platforms.base import SendResult  # type: ignore[import-not-found]
from gateway.session_context import get_session_env  # type: ignore[import-not-found]
from plugins.platforms.feishu import adapter as builtin  # type: ignore[import-not-found]

from .card_mutations import SHUTDOWN_WRITES
from .cardkit.builder import build_background_card, build_cron_card
from .cardkit.interaction_builder import build_approval_card
from .config import Config
from .controller import get_controller
from .feishu import DeliveryUncertain, FeishuClient, FeishuClientConfig
from .interactions.clarify import ClarifyAdapterProxy
from .native_bridge import NativeBridge, NativeTurn
from .native_context import native_home
from .native_history import DeliveryHistory
from .native_hooks import ACTIVE
from .native_reviews import NativeReviews
from .patch import on_feishu_interaction_action, on_feishu_normalize

_logger = logging.getLogger(__name__)
SHUTDOWN_BUDGET_SEC = 5.0
_approval_prompt: ContextVar[Any] = ContextVar("hls_approval_prompt", default=None)
_processing_turn: ContextVar[tuple[Any, NativeTurn] | None] = ContextVar("hls_processing_turn", default=None)


class FeishuStreamingAdapter(builtin.FeishuAdapter):  # type: ignore[misc]
    """Official edit transport avoids native frames mixing prose and tool overlays."""

    SUPPORTS_MESSAGE_EDITING = True
    SUPPORTS_NATIVE_STREAMING = False
    REQUIRES_EDIT_FINALIZE = True

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self.cards = get_controller()
        self._profile_home = self.cards._profile_home
        # Use the credentials already resolved by the built-in adapter. This also
        # covers platform.extra credentials without changing Config precedence.
        self.cards._client = FeishuClient(
            FeishuClientConfig(
                app_id=self._app_id,
                app_secret=self._app_secret,
                base_url="https://open.larksuite.com" if self._domain_name == "lark" else "https://open.feishu.cn",
            ),
            mutations=self.cards._card_mutations,
        )
        self.cards._initialized = True
        self.cards._transport_configured = bool(self._app_id and self._app_secret)
        self.stopping = False
        self.history = DeliveryHistory()
        self.bridge = NativeBridge(self.cards)
        self.bridge.finished = self.history
        self.bridge.on_completed = self._forget_turn
        self.cards._native_owned = lambda session: (
            (turn := self.bridge.messages.get(session.message_id)) is not None and not turn.completed
        )
        self.card_loop: asyncio.AbstractEventLoop | None = None
        self.card_tasks: set[asyncio.Task[Any]] = set()
        self._card_messages: dict[str, NativeTurn] = {}
        self._card_streams: dict[str, str] = {}
        self.reviews = NativeReviews(self.cards)
        self._review_tasks: dict[str, asyncio.Task[Any]] = {}
        self._hermes_lark_interaction_controller = self.cards
        ACTIVE.add(self)

    def _forget_turn(self, turn: NativeTurn) -> None:
        for ident, owner in list(self._card_messages.items()):
            if owner is turn:
                self.history.add(ident, turn.chat_id, "card")
                self._card_messages.pop(ident, None)
                self._card_streams.pop(ident, None)
        self.history.add(turn.card_message_id, turn.chat_id, "card")

    async def _native_send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if self.stopping:
            return SendResult(success=False, error="Adapter is shutting down")
        result = await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)
        if result.success:
            self.history.add(result.message_id, chat_id, "native")
        return result

    @native_home
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        connected = await super().connect(is_reconnect=is_reconnect)
        from .native_runtime import record_adapter

        record_adapter(self.cards._profile_home, connected=bool(connected), adapter=type(self).__name__)
        if connected:
            self.card_loop = asyncio.get_running_loop()
            self.cards._loop = self.card_loop
            for chat in self.reviews.buffer.pending_chats(self.cards._profile_home):
                self.schedule_reviews(chat)
            _logger.info("Hermes Lark native adapter connected: profile=%s", self.cards._profile_home.name)
        return bool(connected)

    @native_home
    async def handle_message(self, event: Any) -> None:
        self.card_loop = asyncio.get_running_loop()
        on_feishu_normalize(message_id=event.message_id, event=event, source=event.source)
        key = self._event_session_key(event)
        if await on_feishu_interaction_action(
            message_id=event.message_id,
            source=event.source,
            event=event,
            session_key=key,
        ):
            return
        await super().handle_message(event)

    @native_home
    async def on_processing_start(self, event: Any) -> None:
        self.card_loop = asyncio.get_running_loop()
        self.cards._loop = self.card_loop
        await self.bridge.drain()
        key = self._event_session_key(event)
        previous = self.bridge.route(key)
        if previous is not None:
            if not previous.terminal:
                previous.interrupted = True
            await self._complete_turn(previous)
        turn = self.bridge.admit(event, key)
        _processing_turn.set((self, turn))
        await super().on_processing_start(event)

    @native_home
    async def on_processing_complete(self, event: Any, outcome: Any) -> None:
        try:
            await self.bridge.drain()
            turn = self.bridge.messages.get(str(event.message_id or ""))
            if turn is not None and not turn.completed:
                turn.terminal = True
                async with self.bridge.lock:
                    delivered = await self._complete_turn(turn)
                if not delivered and not turn.interrupted:
                    from gateway.platforms.event import ProcessingOutcome  # type: ignore[import-not-found]

                    outcome = ProcessingOutcome.FAILURE
            if turn is not None:
                self.schedule_reviews(turn.chat_id)
        finally:
            await super().on_processing_complete(event, outcome)

    async def _complete_turn(self, turn: NativeTurn) -> bool:
        if turn.completed:
            return not turn.fallback or turn.native_fallback_sent
        delivered = await self.bridge.complete(turn) or turn.native_fallback_sent
        if (
            not delivered
            and turn.answer
            and not turn.interrupted
            and not turn.native_fallback_sent
            and not turn.delivery_uncertain
        ):
            result = await self._native_send(
                turn.chat_id,
                turn.answer,
                reply_to=turn.anchor_id,
                metadata={"notify": True},
            )
            turn.native_fallback_sent = bool(result.success)
            delivered = turn.native_fallback_sent
        if delivered or turn.interrupted:
            turn.answer = ""
        return bool(delivered)

    def _turn(self, chat_id: str) -> NativeTurn | None:
        bound = _processing_turn.get()
        if bound is not None and bound[0] is self and bound[1].chat_id == chat_id:
            return bound[1]
        key = str(get_session_env("HERMES_SESSION_KEY") or "")
        turn = self.bridge.route(key)
        return turn if turn is not None and turn.chat_id == chat_id else None

    def _without_cursor(self, content: str, finalize: bool) -> str:
        from gateway.config import StreamingConfig  # type: ignore[import-not-found]

        cursor = StreamingConfig.from_dict(self.cards._cfg._reload().get("streaming", {})).cursor
        return content.removesuffix(cursor) if cursor and not finalize else content

    def prefers_fresh_final_streaming(self, _content: str) -> bool:
        return False

    def max_message_length_for_chat(self, chat_id: str) -> int:
        turn = self._turn(chat_id)
        if self.cards.enabled and turn is not None and not turn.fallback and not turn.completed:
            # The controller owns CardKit rollover. A host plaintext split
            # would replace one cumulative answer with its final 8k tail.
            return 1_000_000_000
        return int(super().max_message_length_for_chat(chat_id))

    @native_home
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if self.stopping:
            return SendResult(success=False, error="Adapter is shutting down")
        md = metadata or {}
        await self.bridge.drain()
        turn = self._turn(chat_id)
        from agent.i18n import t  # type: ignore[import-not-found]

        if self.cards.enabled and md.get("job_id"):
            try:
                assert self.cards._client is not None
                ident = await self.cards._client.send_card_to_chat(
                    chat_id,
                    build_cron_card(content),
                    reply_to_message_id=reply_to or md.get("thread_id"),
                )
                return SendResult(success=True, message_id=ident)
            except DeliveryUncertain as error:
                return SendResult(success=False, error=str(error))
            except Exception:
                _logger.warning("Cron card failed; using native delivery", exc_info=True)
                return await self._native_send(chat_id, content, reply_to=reply_to, metadata=metadata)
        background_template = t("gateway.background.complete_header", preview="{preview}")
        background_prefix, _, background_suffix = background_template.partition("{preview}")
        if self.cards.enabled and background_suffix and content.startswith(background_prefix):
            preview, found, body = content[len(background_prefix) :].partition(background_suffix)
            if found:
                try:
                    assert self.cards._client is not None
                    ident = await self.cards._client.send_card_to_chat(
                        chat_id,
                        build_background_card(preview, body),
                        reply_to_message_id=reply_to or md.get("thread_id"),
                    )
                    return SendResult(success=True, message_id=ident)
                except DeliveryUncertain as error:
                    return SendResult(success=False, error=str(error))
                except Exception:
                    _logger.warning("Background card failed; using native delivery", exc_info=True)
                    return await self._native_send(chat_id, content, reply_to=reply_to, metadata=metadata)
        if self.cards.enabled and md.get("_interim_send"):
            from .agent_status import AgentStatusEvent, conversation_key

            if content.startswith(t("display.review.summary_callback", summary="")):
                # The native notification remains a fallback if its observer failed.
                await self.reviews.deliver(chat_id)
                ref = self.cards._agent_status_manager.registry.get(conversation_key(chat_id))
                if not self.reviews.covers(chat_id, content) and (ref is None or ref.agent_status != content):
                    await self.cards._deliver_agent_status(
                        event=AgentStatusEvent.background_review(
                            conversation_key=conversation_key(chat_id),
                            text=content,
                        ),
                        chat_id=chat_id,
                    )
                return SendResult(success=True, message_id=ref.message_id if ref else None)
            if turn is not None and not turn.completed:
                heartbeat = re.escape(t("gateway.progress.working_heartbeat", minutes="{minutes}", detail="{detail}"))
                heartbeat = heartbeat.replace(re.escape("{minutes}"), r"(?P<minutes>\d+)")
                heartbeat = heartbeat.replace(re.escape("{detail}"), r".*")
                match = re.fullmatch(heartbeat, content, flags=re.DOTALL)
                elapsed = float(match.group("minutes")) * 60 if match and "minutes" in match.groupdict() else None
                if elapsed is None:
                    from gateway.display_config import resolve_display_setting  # type: ignore[import-not-found]
                    from gateway.status_phrases import resolve_status_phrase_catalog  # type: ignore[import-not-found]

                    config = self.cards._cfg._reload()
                    mode = resolve_display_setting(config, "feishu", "long_running_notifications", True)
                    catalog = resolve_status_phrase_catalog(config, "feishu")
                    if str(mode).lower() == "generic" and content in catalog["status"]:
                        elapsed = float(int((time.monotonic() - turn.started_at) // 60) * 60)
                if elapsed is not None:
                    from gateway.run import _current_max_iterations  # type: ignore[import-not-found]

                    if self.cards.on_long_running_progress(
                        message_id=turn.message_id,
                        elapsed_seconds=elapsed,
                        iteration=turn.iteration,
                        max_iterations=_current_max_iterations(),
                    ):
                        return SendResult(success=True)
                    return await self._native_send(chat_id, content, reply_to=reply_to, metadata=metadata)
        if turn is not None and turn.completed and (md.get("expect_edits") or md.get("notify")):
            return SendResult(success=True, message_id=turn.card_message_id)
        if self.cards.enabled and turn is not None and not turn.fallback and not turn.completed:
            if md.get("_interim_send") and not md.get("non_conversational"):
                if await self.bridge.ensure_card(turn):
                    available = [
                        key
                        for key, text in turn.expected_answers.items()
                        if text.strip() and key not in turn.stream_iterations.values()
                    ]
                    turn.stream_iterations["preview:" + uuid.uuid4().hex] = min(available, default=turn.iteration)
                    if self.cards.on_thinking(
                        message_id=turn.message_id,
                        text=content,
                        api_mode=turn.api_mode,
                        source="interim_commentary",
                    ):
                        return SendResult(success=True, message_id=turn.card_message_id)
                    return await self._native_send(chat_id, content, reply_to=reply_to, metadata=metadata)
            elif md.get("expect_edits") or md.get("notify"):
                final = bool(md.get("notify")) and not md.get("expect_edits")
                stream_id = uuid.uuid4().hex
                if await self.bridge.frame(
                    turn,
                    self._without_cursor(content, final),
                    stream_id=stream_id,
                    finalize=final,
                ):
                    self._card_messages[turn.card_message_id] = turn
                    self._card_streams[turn.card_message_id] = stream_id
                    return SendResult(success=True, message_id=turn.card_message_id)
                turn.fallback = True
            elif turn.turn_id and any(content.startswith(prefix) for prefix in turn.native_tool_prefixes):
                # Gateway tool chrome is represented by the official tool hooks.
                return SendResult(success=True, message_id=None)
        result = await self._native_send(chat_id, content, reply_to=reply_to, metadata=metadata)
        if turn is not None and turn.fallback and md.get("notify"):
            turn.native_fallback_sent = bool(result.success)
        return result

    @native_home
    async def send_or_update_status(
        self,
        chat_id: str,
        status_key: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        # Native status is independent from tool chrome and model commentary.
        return await self._native_send(chat_id, content, metadata=metadata)

    @native_home
    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if self.stopping:
            return SendResult(success=False, error="Adapter is shutting down")
        turn = self._card_messages.get(message_id)
        if turn is None:
            previous = self.history.get(message_id)
            if previous is None or previous.chat_id != chat_id:
                return SendResult(success=False, error="Unknown message ownership")
            if previous.kind == "card":
                return SendResult(success=True, message_id=message_id)
            if previous.kind == "native":
                return await super().edit_message(chat_id, message_id, content, finalize=finalize)
            return SendResult(success=False, error="Message is not an owned outbound delivery")
        if turn.completed:
            return SendResult(success=True, message_id=message_id)
        if turn.chat_id != chat_id or turn.fallback:
            return SendResult(success=False, error="Card stream is no longer active")
        if await self.bridge.frame(
            turn,
            self._without_cursor(content, finalize),
            stream_id=self._card_streams[message_id],
            finalize=finalize,
        ):
            return SendResult(success=True, message_id=message_id)
        return SendResult(success=False, error="CardKit delivery failed")

    @native_home
    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: list[str] | None,
        clarify_id: str,
        session_key: str,
        metadata: dict[str, Any] | None = None,
        multi_select: bool = False,
    ) -> Any:
        turn = self.bridge.route(session_key)
        owner_ids = turn.owner_ids if turn else frozenset()
        # The proxy annotates its wrapped adapter; a super object is read-only.
        # Supply only the native fallback method so the proxy cannot recurse.
        from types import SimpleNamespace

        proxy = ClarifyAdapterProxy(
            SimpleNamespace(send_clarify=super().send_clarify),
            self.cards,
            owner_ids,
        )
        return await proxy.send_clarify(
            chat_id,
            question,
            choices,
            clarify_id,
            session_key,
            metadata=metadata,
            multi_select=multi_select,
        )

    @native_home
    async def interrupt_session_activity(
        self,
        session_key: str,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self.bridge.drain()
            turn = self.bridge.route(session_key)
            if turn is not None:
                turn.interrupted = turn.terminal = True
                async with self.bridge.lock:
                    await self.bridge.complete(turn)
        finally:
            await super().interrupt_session_activity(session_key, chat_id, metadata)

    def schedule_reviews(self, chat_id: str) -> None:
        if self.stopping:
            return
        if chat_id in self._review_tasks:
            return

        async def deliver() -> None:
            try:
                for delay in (0.25, 1, 3, 10):
                    await asyncio.sleep(delay)
                    delivered = await self.reviews.deliver(chat_id)
                    if delivered and not self.reviews.buffer.snapshot(self.cards._profile_home, chat_id).receipts:
                        break
            finally:
                self._review_tasks.pop(chat_id, None)

        task = asyncio.create_task(deliver())
        self._review_tasks[chat_id] = task
        self.card_tasks.add(task)
        task.add_done_callback(self.card_tasks.discard)
        task.add_done_callback(self.cards._on_bg_task_done)

    async def _send_exec_approval_prompt(self, prompt: Any) -> SendResult:
        token = _approval_prompt.set(prompt)
        try:
            return await super()._send_exec_approval_prompt(prompt)
        finally:
            _approval_prompt.reset(token)

    async def _send_interactive_card(self, chat_id: str, card: dict[str, Any], *args: Any, **kwargs: Any) -> SendResult:
        if self.stopping:
            return SendResult(success=False, error="Adapter is shutting down")
        prompt = _approval_prompt.get()
        if prompt is not None and self.cards.approval_card_enabled:
            from .interactions.approval import transform_approval_card

            card = transform_approval_card(
                registry=self.cards.approval_registry,
                adapter=self,
                card=card,
                chat_id=chat_id,
                command=prompt.command,
                session_key=prompt.session_key,
                description=prompt.description,
            )
        return await super()._send_interactive_card(chat_id, card, *args, **kwargs)

    @staticmethod
    def _build_resolved_approval_card(*, choice: str, user_name: str, body: str | None = None) -> dict[str, Any]:
        return build_approval_card(
            command=body or "",
            description="",
            buttons=[],
            status="denied" if choice == "deny" else "approved",
            decision=choice,
        )

    def _handle_approval_card_action(self, *, event: Any, action_value: dict[str, Any], loop: Any) -> Any:
        approval_id = action_value.get("approval_id")
        owned = self.cards.approval_registry.get(id(self), approval_id)
        if owned is None:
            return super()._handle_approval_card_action(event=event, action_value=action_value, loop=loop)
        state = self._approval_state.get(approval_id)
        if not state:
            return self._card_response()
        checked = self._validate_card_action(event=event, state=state, label="approval", ident=approval_id)
        if checked is None:
            return self._card_response()
        open_id, chat_id, _name, user_id, _union_id = checked
        from gateway.approval_actor import approval_refusal, refusal_text  # type: ignore[import-not-found]

        refusal = approval_refusal(
            state["session_key"],
            user_id or open_id,
            extra=self.config.extra,
            request_id=state.get("request_id"),
        )
        if refusal:
            return self._card_toast(refusal_text(refusal))
        from .interactions.approval import handle_approval_action

        choice = builtin._APPROVAL_CHOICE_MAP.get(action_value.get("hermes_action"), "deny")
        result = handle_approval_action(
            registry=self.cards.approval_registry,
            adapter=self,
            approval_id=approval_id,
            action_value=action_value,
            choice=choice,
            open_id=open_id,
            callback_chat_id=chat_id,
            operator_authorized=True,
            request_actor=user_id or open_id,
        )
        return self._card_response(result.card if result else None)

    @native_home
    async def disconnect(self) -> None:
        try:
            await self._shutdown_cards()
        finally:
            ACTIVE.discard(self)
            try:
                from .native_runtime import record_adapter

                record_adapter(self.cards._profile_home, connected=False, adapter=type(self).__name__)
            except Exception:
                _logger.warning("Could not record adapter shutdown", exc_info=True)
            finally:
                await super().disconnect()

    async def _shutdown_cards(self) -> None:
        self.stopping = self.bridge.stopping = True
        ACTIVE.discard(self)
        self.cards._card_mutations.stopping = True
        self.cards._card_mutations.shutdown_deadline = time.monotonic() + SHUTDOWN_BUDGET_SEC
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SHUTDOWN_BUDGET_SEC
        for task in list(self.card_tasks):
            task.cancel()
        self.bridge._queue.clear()
        sessions = {id(s): s for s in self.cards._sessions.values()}
        for session in sessions.values():
            session.flush.mark_completed()
            if session.create_task is not None and not session.create_task.done():
                session.create_task.cancel()
            if session.image_resolver:
                session.image_resolver.cancel_pending()

        async def seal(turn: NativeTurn) -> None:
            token = SHUTDOWN_WRITES.set(True)
            try:
                turn.interrupted = True
                await self.bridge.complete(turn)
            except Exception:
                _logger.warning("Card shutdown seal failed", exc_info=True)
            finally:
                SHUTDOWN_WRITES.reset(token)

        tasks = [asyncio.create_task(seal(turn)) for turn in list(self.bridge.turns.values()) if not turn.completed]
        try:
            if tasks:
                _done, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline - loop.time()))
                for task in pending:
                    task.cancel()
                    task.add_done_callback(self.cards._on_bg_task_done)
                if pending:
                    _logger.warning("Card shutdown budget exhausted: pending=%d", len(pending))
        finally:
            for session in sessions.values():
                self.cards._cleanup_session(session)
            self.bridge.turns.clear()
            self.bridge.messages.clear()
            self._card_messages.clear()
            self._card_streams.clear()


async def standalone_send(
    config: Any,
    chat_id: str,
    content: str,
    *,
    thread_id: str | None = None,
    media_files: Any = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """Use the same built-in credential resolution without opening a WebSocket."""
    if not Config().enabled:
        return dict(
            await builtin._standalone_send(
                config,
                chat_id,
                content,
                thread_id=thread_id,
                media_files=media_files,
                force_document=force_document,
            )
        )
    adapter = builtin.FeishuAdapter(config)
    client = FeishuClient(
        FeishuClientConfig(
            app_id=adapter._app_id,
            app_secret=adapter._app_secret,
            base_url="https://open.larksuite.com" if adapter._domain_name == "lark" else "https://open.feishu.cn",
        )
    )
    if content.strip():
        try:
            ident = await client.send_card_to_chat(
                chat_id,
                build_cron_card(content),
                reply_to_message_id=thread_id,
            )
        except DeliveryUncertain as error:
            return {"success": False, "platform": "feishu", "chat_id": chat_id, "error": str(error)}
        except Exception:
            return dict(
                await builtin._standalone_send(
                    config,
                    chat_id,
                    content,
                    thread_id=thread_id,
                    media_files=media_files,
                    force_document=force_document,
                )
            )
        if not media_files:
            return {"success": True, "platform": "feishu", "chat_id": chat_id, "message_id": ident}
    # Media delivery reuses the built-in path without re-sending the card's text.
    return dict(
        await builtin._standalone_send(
            config,
            chat_id,
            "",
            thread_id=thread_id,
            media_files=media_files,
            force_document=force_document,
        )
    )
