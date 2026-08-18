"""StreamCardController — 流式卡片主控制器（单例）."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import Future as ConcurrentFuture
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import Config, hermes_home
from .feishu import (
    FeishuClient,
    FeishuClientConfig,
)
from .interactions.clarify import (
    ClarifySendResult,
    handle_clarify_action,
    send_clarify_card,
)
from .interactions.registry import ApprovalCardRegistry, ClarifyCardRegistry
from .quota import _quota_color
from .streaming.controller import StreamingController
from .streaming.segments import SegmentType
from .streaming.session import CardSession, SessionState
from .streaming.text import strip_reasoning_tags

_logger = logging.getLogger("hermes_lark_streaming")
_CARD_CREATION_WAIT_SEC = 10.0

def _weekly_quota_window(rate_limit: object) -> dict[str, Any] | None:
    """Select the structured seven-day quota window, independent of names."""
    if not isinstance(rate_limit, dict):
        return None

    candidates: list[object] = []
    # Keep the API's conventional order when both windows are weekly, while
    # making the decision solely from the structured duration metadata.
    for key in ("primary_window", "secondary_window"):
        if key in rate_limit:
            candidates.append(rate_limit[key])
    for key, value in rate_limit.items():
        if key not in {"primary_window", "secondary_window"}:
            candidates.append(value)

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        raw_duration = candidate.get("limit_window_seconds")
        if raw_duration is None:
            continue
        try:
            duration = int(raw_duration) if not isinstance(raw_duration, bool) else 0
        except (TypeError, ValueError):
            continue
        if duration == 604800:
            return candidate
    return None


def _fetch_gpt_quota_footer(model: str) -> dict[str, Any]:
    """Return structured Codex/GPT quota metadata for the footer.

    Uses Hermes' credential pool rather than the singleton Codex auth store, so
    it works for profiles/accounts that only have pooled credentials. Fail-open:
    any auth/API/parsing error returns an empty dict and the footer fields are
    hidden by the card builder.
    """
    normalized_model = (model or "").lower()
    if not any(marker in normalized_model for marker in ("gpt", "codex", "openai")):
        return {}

    try:
        import httpx
        from agent.credential_pool import load_pool  # type: ignore[import-not-found]

        pool = load_pool("openai-codex")
        cred = pool.select()
        token = str(getattr(cred, "access_token", "") or "").strip() if cred else ""
        if not token:
            return {}

        base_url = str(getattr(cred, "base_url", "") or "https://chatgpt.com/backend-api/codex").strip().rstrip("/")
        if base_url.endswith("/codex"):
            base_url = base_url[: -len("/codex")]
        usage_url = base_url + ("/wham/usage" if "/backend-api" in base_url else "/api/codex/usage")

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "codex-cli",
        }
        account_id = (getattr(cred, "extra", None) or {}).get("account_id")
        if account_id:
            headers["ChatGPT-Account-Id"] = str(account_id)

        response = httpx.get(usage_url, headers=headers, timeout=5.0)
        response.raise_for_status()
        payload = response.json() or {}
        rate_limit = payload.get("rate_limit") or {}
        window = _weekly_quota_window(rate_limit)
        if window is None:
            return {}
        used = window.get("used_percent")
        if used is None:
            return {}
        try:
            used_percent = float(used)
        except (TypeError, ValueError):
            return {}
        if not 0 <= used_percent <= 100:
            return {}
        remaining = max(0, min(100, round(100 - used_percent)))
        metadata: dict[str, Any] = {
            "remaining": f"<font color='{_quota_color(remaining)}'>{remaining}%</font>"
        }
        if window.get("reset_at") not in (None, ""):
            metadata["reset_at"] = window["reset_at"]
        return metadata
    except Exception:
        return {}


def _turn_usage_footer_data(usage: dict | None, *, api_calls: object = 0) -> dict[str, int]:
    """Select reliable current-turn Hermes usage fields for Run Details."""

    def positive_int(value: object) -> int:
        if not isinstance(value, (str, int, float)):
            return 0
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            return 0
        return parsed if parsed > 0 else 0

    data: dict[str, int] = {}
    if isinstance(usage, dict):
        uncached_input = positive_int(usage.get("input_tokens"))
        cache_read = positive_int(usage.get("cache_read_tokens"))
        cache_write = positive_int(usage.get("cache_write_tokens"))
        prompt_tokens = positive_int(usage.get("prompt_tokens"))
        if not prompt_tokens:
            prompt_tokens = uncached_input + cache_read + cache_write
        output_tokens = positive_int(
            usage.get("output_tokens") or usage.get("completion_tokens")
        )
        reasoning_tokens = positive_int(usage.get("reasoning_tokens"))

        if prompt_tokens:
            data["input_tokens"] = prompt_tokens
            data["cache_prompt_tokens"] = prompt_tokens
        if output_tokens:
            data["output_tokens"] = output_tokens
        if cache_read:
            data["cache_read_tokens"] = cache_read
        if cache_write:
            data["cache_write_tokens"] = cache_write
        if reasoning_tokens:
            data["reasoning_tokens"] = reasoning_tokens

    call_count = positive_int(api_calls)
    if call_count:
        data["api_calls"] = call_count
    return data


class StreamCardController(StreamingController):
    """流式卡片控制器 — 管理多条消息的卡片生命周期."""

    def __init__(self, profile_home: Path | None = None) -> None:
        self._profile_home = (profile_home or hermes_home()).resolve()
        self._cfg = Config(self._profile_home)
        self._client: FeishuClient | None = None
        self._sessions: dict[str, CardSession] = {}
        self._session_keys: dict[str, CardSession] = {}
        self._interrupt_map: dict[str, str] = {}
        self._initialized = False
        self._init_lock = threading.Lock()
        self._session_ttl = self._cfg.card_duration_sec
        self._loop: asyncio.AbstractEventLoop | None = None
        self._text_fallback_needed: set[str] = set()
        self._text_fallback_aliases: dict[str, set[str]] = {}
        self._unscoped_enabled: bool | None = None
        self._clarify_registry = ClarifyCardRegistry()
        self._approval_registry = ApprovalCardRegistry()

    @property
    def enabled(self) -> bool:
        unscoped = self._needs_fallback_scope()
        if unscoped and self._unscoped_enabled is not None:
            return self._unscoped_enabled
        with self._credential_scope():
            enabled = self._cfg.enabled and bool(self._cfg.feishu_app_id or self._cfg.env_app_id)
        if unscoped and enabled:
            self._unscoped_enabled = True
        return enabled

    @property
    def clarify_card_enabled(self) -> bool:
        return self.enabled and self._cfg.clarify_style == "card"

    @property
    def approval_card_enabled(self) -> bool:
        return self.enabled and self._cfg.confirmation_style == "openclaw"

    @property
    def approval_registry(self) -> ApprovalCardRegistry:
        return self._approval_registry

    @staticmethod
    def _needs_fallback_scope() -> bool:
        try:
            from agent.secret_scope import current_secret_scope, is_multiplex_active  # type: ignore[import-not-found]
        except ImportError:
            return False
        return is_multiplex_active() and current_secret_scope() is None

    @contextmanager
    def _credential_scope(self) -> Iterator[None]:
        try:
            from agent.secret_scope import (  # type: ignore[import-not-found]
                build_profile_secret_scope,
                current_secret_scope,
                is_multiplex_active,
                reset_secret_scope,
                set_secret_scope,
            )
        except ImportError:
            yield
            return
        if not is_multiplex_active() or current_secret_scope() is not None:
            yield
            return
        token = set_secret_scope(build_profile_secret_scope(self._profile_home))
        try:
            yield
        finally:
            reset_secret_scope(token)

    async def _ensure_init(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with self._credential_scope():
                app_id = self._cfg.feishu_app_id or self._cfg.env_app_id
                app_secret = self._cfg.feishu_app_secret or self._cfg.env_app_secret
                if not app_id or not app_secret:
                    raise RuntimeError("feishu credentials not configured")
                self._client = FeishuClient(
                    FeishuClientConfig(
                        app_id=app_id,
                        app_secret=app_secret,
                        base_url=self._cfg.feishu_base_url,
                    )
                )
            self._initialized = True

    async def send_clarify_card(
        self,
        *,
        chat_id: str,
        question: str,
        choices: list[str],
        clarify_id: str,
        session_key: str,
        owner_user_ids: frozenset[str],
        metadata: dict[str, Any] | None = None,
    ) -> ClarifySendResult:
        """Deliver a Feishu clarify card without replacing Hermes state."""
        if self._cfg.clarify_style != "card" or not choices:
            return ClarifySendResult(False, error="clarify card disabled or unsupported")
        await self._ensure_init()
        if self._client is None:
            return ClarifySendResult(False, error="Feishu client unavailable")
        return await send_clarify_card(
            client=self._client,
            registry=self._clarify_registry,
            chat_id=chat_id,
            question=question,
            choices=choices,
            clarify_id=clarify_id,
            session_key=session_key,
            owner_user_ids=owner_user_ids,
            metadata=metadata,
        )

    async def on_clarify_action(
        self,
        *,
        raw_message: Any,
        source_chat_id: str,
        session_key: str,
    ) -> bool:
        """Bridge a genuine Feishu action into Hermes' pending registry."""
        return await handle_clarify_action(
            client=self._client,
            registry=self._clarify_registry,
            raw_message=raw_message,
            source_chat_id=source_chat_id,
            session_key=session_key,
        )

    def clarify_id_for_card_message(self, *, chat_id: str, card_msg_id: str) -> str:
        """Resolve a form callback's clarify id when Feishu omits button value."""
        state = self._clarify_registry.find_by_card_message(chat_id, card_msg_id)
        return state.clarify_id if state is not None else ""

    def _get_loop(self) -> asyncio.AbstractEventLoop | None:
        """获取事件循环，缓存以便跨线程复用."""
        try:
            loop = asyncio.get_running_loop()
            self._loop = loop
            return loop
        except RuntimeError:
            pass
        if self._loop is not None and not self._loop.is_closed():
            return self._loop
        return None

    def _get_active_session(self, message_id: str) -> CardSession | None:
        """获取非终态的活跃 session，不存在或已终态返回 None."""
        session = self._sessions.get(message_id)
        if session is None or session.state.is_terminal:
            return None
        return session

    def _fire_and_forget(
        self,
        coro: Coroutine[Any, Any, Any],
        loop: asyncio.AbstractEventLoop,
    ) -> asyncio.Future[Any] | ConcurrentFuture | None:
        try:
            task = loop.create_task(coro)
            task.add_done_callback(self._on_bg_task_done)
            return task
        except RuntimeError:
            try:
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                fut.add_done_callback(self._on_bg_task_done)
                return fut
            except Exception:
                _logger.debug("fire_and_forget failed", exc_info=True)
                return None

    def on_message_started(
        self,
        *,
        message_id: str | None,
        chat_id: str,
        anchor_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        """消息处理开始 — 创建会话 + 发占位卡片."""
        if not self.enabled:
            return
        if not message_id:
            _logger.warning("on_message_started: missing message_id, chat=%s", chat_id[:12])
            return
        if message_id in self._sessions:
            return

        self._prune_stale_sessions()

        loop = self._get_loop()
        if loop is None:
            _logger.warning("no event loop available, skipping: msg=%s", message_id[:12])
            return
        session = CardSession(message_id, chat_id, loop)
        session.session_key = session_key
        self._sessions[message_id] = session
        if session_key:
            self._session_keys[session_key] = session
        if anchor_id and anchor_id != message_id:
            session.anchor_id = anchor_id
            self._sessions[anchor_id] = session
        _logger.info("session created: msg=%s chat=%s anchor=%s", message_id[:12], chat_id[:12], (anchor_id or "")[:12])

        session.create_task = self._fire_and_forget(self._do_create_card(session), loop)

    def on_turn_usage(
        self,
        *,
        message_id: str,
        usage: dict | None,
        api_calls: object = 0,
    ) -> None:
        """Store Hermes' canonical current-turn usage on the active card session."""
        if not self.enabled:
            return
        session = self._get_active_session(message_id)
        if session is None:
            redirected_id = self._interrupt_map.get(message_id)
            session = self._get_active_session(redirected_id) if redirected_id else None
        if session is None:
            return
        session.footer.update(_turn_usage_footer_data(usage, api_calls=api_calls))

    def _mark_text_fallback_needed(self, session: CardSession) -> None:
        keys = {session.message_id}
        if session.anchor_id:
            keys.add(session.anchor_id)
        self._text_fallback_needed.update(keys)
        for key in keys:
            self._text_fallback_aliases[key] = set(keys)

    def consume_text_fallback(self, message_id: str) -> bool:
        """Return whether gateway should undo already_sent and deliver plain text."""
        if message_id not in self._text_fallback_needed:
            return False
        keys = self._text_fallback_aliases.pop(message_id, {message_id})
        for key in keys:
            self._text_fallback_needed.discard(key)
            self._text_fallback_aliases.pop(key, None)
        return True

    def on_thinking(
        self,
        *,
        message_id: str,
        text: str,
        api_mode: str = "",
        source: str = "",
    ) -> bool:
        """Handle tagged thinking deltas and classified interim commentary."""
        if not self.enabled:
            return False
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_thinking"):
            return False

        if session.segment_state is None:
            return False
        return self._on_thinking_segment(
            session,
            text,
            api_mode=api_mode,
            source=source,
        )

    def on_reasoning(self, *, message_id: str, text: str, api_mode: str = "") -> bool:
        """Route native reasoning presentation by its Hermes API-mode semantics."""
        if not self.enabled:
            return False
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_reasoning"):
            return False

        if not self._cfg.show_reasoning:
            return False

        if session.segment_state is None:
            return False

        self._record_native_reasoning(session, text, api_mode=api_mode)
        self._schedule_flush(session)
        return True

    def on_tool_update(
        self,
        *,
        message_id: str,
        tool_name: str,
        status: str,
        detail: str = "",
    ) -> bool:
        """工具调用事件."""
        if not self.enabled:
            return False
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_tool_update"):
            return False
        if session.segment_state is None:
            return False

        if status in ("running", "started", "tool.started"):
            self._pause_merged_reasoning(session)
            session.tool_use.record_start(tool_name, detail)
        else:
            is_error = status in ("error", "failed")
            session.tool_use.record_end(
                tool_name,
                error=detail if is_error else "",
                output="" if is_error else detail,
            )

        session.segment_state.on_tool_event(len(session.tool_use.build_display_steps()))
        session.tool_panel.note_tool_event()
        self._schedule_flush(session)
        return True

    def on_answer(self, *, message_id: str, text: str) -> bool:
        """答案文本增量（流式）."""
        if not self.enabled:
            return False
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_answer"):
            return False
        if session.segment_state is None:
            return False

        answer_text = strip_reasoning_tags(text)
        if not answer_text:
            return False

        _logger.debug(
            "stream lane=answer msg=%s len=%d head=%r",
            session.message_id[:12],
            len(answer_text),
            answer_text[:120],
        )
        self._pause_merged_reasoning(session)
        if answer_text.strip():
            self._start_final(session, source="on_answer")
        self._append_answer_segment(session, answer_text)
        self._schedule_flush(session)
        return True

    def on_aborted(self, *, message_id: str) -> None:
        """用户 /stop 导致消息被中断."""
        if not self.enabled:
            return
        session = self._get_active_session(message_id)
        if session is None:
            return

        progress = getattr(session, "progress", None)
        if progress is not None:
            progress.clear()
        self._start_final(session, source="abort")
        session.state = SessionState.ABORTED
        session.flush.mark_completed()
        _logger.info("on_aborted: msg=%s state=ABORTED", message_id[:12])

        self._complete_session(session)

    async def on_session_aborted(self, *, session_key: str, stop_command: bool = False) -> bool:
        """Terminate the active card bound to a Hermes session key."""
        if not self.enabled or not session_key:
            return False
        session = self._session_keys.pop(session_key, None)
        if session is None or session.state.is_terminal:
            return False

        session.progress.clear()
        self._start_final(session, source="abort")
        session.state = SessionState.ABORTED
        if stop_command:
            session.footer["stop_continue_hint"] = True
        session.flush.mark_completed()
        _logger.info("on_session_aborted: msg=%s state=ABORTED", session.message_id[:12])

        return await self._complete_session_after_creation(session, require_card=True)

    def on_interrupted(
        self,
        *,
        old_message_id: str,
        new_message_id: str,
        chat_id: str,
        anchor_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        """用户发送新消息导致前一条消息被中断 — abort A + create B."""
        if not self.enabled:
            return

        old_session = self._get_active_session(old_message_id)
        session_key = session_key or (old_session.session_key if old_session is not None else None)
        if old_session is not None:
            old_session.progress.clear()
            self._start_final(old_session, source="abort")
            old_session.state = SessionState.ABORTED
            old_session.flush.mark_completed()
            _logger.info(
                "on_interrupted: abort old msg=%s",
                old_message_id[:12],
            )
            self._complete_session(old_session)

        existing = self._sessions.get(new_message_id)
        if existing is None or existing.state.is_terminal:
            loop = self._get_loop()
            if loop is not None:
                reply_anchor_id = anchor_id if anchor_id and anchor_id != new_message_id else None
                session = CardSession(new_message_id, chat_id, loop)
                session.anchor_id = reply_anchor_id
                session.session_key = session_key
                self._sessions[new_message_id] = session
                if session_key:
                    self._session_keys[session_key] = session
                if reply_anchor_id:
                    self._sessions[reply_anchor_id] = session
                _logger.info(
                    "on_interrupted: create new msg=%s chat=%s anchor=%s",
                    new_message_id[:12],
                    chat_id[:12],
                    (reply_anchor_id or new_message_id)[:12],
                )
                session.create_task = self._fire_and_forget(self._do_create_card(session), loop)

        self._interrupt_map[old_message_id] = new_message_id
        for key, val in list(self._interrupt_map.items()):
            if val == old_message_id:
                self._interrupt_map[key] = new_message_id

    async def on_completed_wait(
        self,
        *,
        message_id: str,
        answer: str = "",
        is_error: bool = False,
        duration: float = 0.0,
        model: str = "",
        tokens: dict | None = None,
        context: dict | None = None,
    ) -> bool:
        """消息处理完成，并等待卡片真正收尾后返回是否已发送."""
        if not self.enabled:
            return False
        session = self._completion_session(message_id)
        if session is None:
            return False
        message_id = session.message_id

        if not await self._wait_for_card_creation(session):
            if session.has_card:
                _logger.info("on_completed_wait: msg=%s card creation not ready but card exists", message_id[:12])
            else:
                _logger.info("on_completed_wait: msg=%s card creation not ready, yielding to gateway", message_id[:12])
                self._mark_text_fallback_needed(session)
            self._cleanup_session(session)
            return False

        if session.state == SessionState.FAILED:
            if session.has_card:
                _logger.info("on_completed_wait: msg=%s state=FAILED but card exists", message_id[:12])
            else:
                _logger.info("on_completed_wait: msg=%s state=FAILED, yielding to gateway", message_id[:12])
                self._mark_text_fallback_needed(session)
            self._cleanup_session(session)
            return False

        if not session.has_card:
            _logger.info("on_completed_wait: msg=%s has no card, yielding to gateway", message_id[:12])
            self._mark_text_fallback_needed(session)
            self._cleanup_session(session)
            return False

        _logger.debug(
            "on_completed_wait: msg=%s has_card=%s state=%s",
            message_id[:12],
            session.has_card,
            session.state,
        )

        self._apply_completion_payload(
            session=session,
            answer=answer,
            duration=duration,
            model=model,
            tokens=tokens,
            context=context,
        )
        if is_error:
            self._start_final(session, source="error")
            session.mark_failed()

        return await self._complete_session_wait(session)

    def on_long_running_progress(
        self,
        *,
        message_id: str,
        elapsed_seconds: float,
        iteration: int | None = None,
        max_iterations: int | None = None,
    ) -> bool:
        """Own a real Hermes heartbeat only when an active card can display it."""
        if not self.enabled or self._cfg.progress_mode != "card":
            return False
        session = self._get_active_session(message_id)
        if (
            session is None
            or session.state != SessionState.STREAMING
            or not session.card_id
            or session.guard.should_skip("on_long_running_progress")
        ):
            return False
        if not session.progress.available:
            return False
        session.progress.note_heartbeat(
            elapsed_seconds,
            iteration=iteration,
            max_iterations=max_iterations,
        )
        self._schedule_flush(session)
        return True

    def on_cron_deliver(
        self,
        *,
        chat_id: str,
        content: str,
        loop: asyncio.AbstractEventLoop | None,
        task_name: str = "",
        run_time: str = "",
    ) -> bool:
        """Cron 推送 — 包装为静态卡片发送，成功返回 True."""
        if not self.enabled or not content or not chat_id:
            return False
        coroutine = self._do_cron_deliver(
            chat_id, content, task_name=task_name, run_time=run_time
        )
        try:
            if loop is not None and loop.is_running() and not loop.is_closed():
                try:
                    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
                except Exception:
                    coroutine.close()
                    raise
                future.result(timeout=30)
            else:
                asyncio.run(coroutine)
            _logger.info("cron card delivered: chat=%s len=%d", chat_id[:12], len(content))
            return True
        except Exception:
            _logger.warning("cron card delivery failed", exc_info=True)
            return False

    async def on_background_deliver(
        self,
        *,
        chat_id: str,
        preview: str,
        content: str,
        reply_to_message_id: str | None = None,
    ) -> bool:
        """Background 任务完成推送 — 包装为静态卡片发送，成功返回 True."""
        if not self.enabled or not content or not chat_id:
            return False
        try:
            await self._do_background_deliver(
                chat_id,
                preview,
                content,
                reply_to_message_id=reply_to_message_id,
            )
            _logger.info("background card delivered: chat=%s len=%d", chat_id[:12], len(content))
            return True
        except Exception:
            _logger.warning("background card delivery failed", exc_info=True)
            return False

    def defer_background_review(
        self,
        *,
        message_id: str,
        text: str,
        sender: Callable[[str], Any],
    ) -> bool:
        """暂存 Hermes background review 通知，等卡片收尾后再发送."""
        if not self.enabled or not text or not callable(sender):
            return False
        session = self._get_active_session(message_id)
        if session is None:
            return False
        with session.deferred_background_review_lock:
            if session.deferred_background_review_closed:
                return False
            session.deferred_background_reviews.append((text, sender))
        return True

    def _flush_deferred_background_reviews(self, session: CardSession) -> None:
        lock = getattr(session, "deferred_background_review_lock", None)
        reviews = getattr(session, "deferred_background_reviews", None)
        if lock is None or reviews is None:
            return
        with lock:
            session.deferred_background_review_closed = True
            pending = list(reviews)
            reviews.clear()
        for text, sender in pending:
            try:
                sender(text)
            except Exception:
                _logger.debug("background review sender failed", exc_info=True)

    def _cleanup(self, message_id: str) -> None:
        session = self._sessions.pop(message_id, None)
        if session is None:
            return
        anchor = getattr(session, "anchor_id", None)
        if anchor and self._sessions.get(anchor) is session:
            del self._sessions[anchor]
        session_key = getattr(session, "session_key", None)
        if session_key and self._session_keys.get(session_key) is session:
            del self._session_keys[session_key]
        stale_keys = [k for k, v in self._interrupt_map.items() if v == message_id]
        for k in stale_keys:
            del self._interrupt_map[k]
        progress = getattr(session, "progress", None)
        if progress is not None:
            progress.clear()
        session.flush.mark_completed()
        if session.image_resolver:
            session.image_resolver.cancel_pending()

    def _cleanup_session(self, session: CardSession) -> None:
        if self._sessions.get(session.message_id) is session:
            self._sessions.pop(session.message_id, None)
        anchor = session.anchor_id
        if anchor and self._sessions.get(anchor) is session:
            del self._sessions[anchor]
        session_key = session.session_key
        if session_key and self._session_keys.get(session_key) is session:
            del self._session_keys[session_key]
        stale_keys = [key for key, value in self._interrupt_map.items() if value == session.message_id]
        for key in stale_keys:
            del self._interrupt_map[key]
        session.progress.clear()
        session.flush.mark_completed()
        if session.image_resolver:
            session.image_resolver.cancel_pending()

    def _completion_session(self, message_id: str) -> CardSession | None:
        session = self._sessions.get(message_id)
        if session is not None and (not session.state.is_terminal or session.state == SessionState.FAILED):
            return session

        redirected_id = self._interrupt_map.pop(message_id, None)
        if redirected_id is not None:
            _logger.info(
                "on_completed: redirect msg=%s -> msg=%s",
                message_id[:12],
                redirected_id[:12],
            )
            redirected = self._sessions.get(redirected_id)
            if redirected is not None and not redirected.state.is_terminal:
                return redirected
        return None

    async def _wait_for_card_creation(self, session: CardSession) -> bool:
        task = session.create_task
        if task is None:
            return True
        try:
            if isinstance(task, asyncio.Future):
                await asyncio.wait_for(task, timeout=_CARD_CREATION_WAIT_SEC)
            else:
                await asyncio.wait_for(asyncio.wrap_future(task), timeout=_CARD_CREATION_WAIT_SEC)
            return True
        except TimeoutError:
            _logger.warning(
                "card creation timed out: msg=%s timeout=%.1fs",
                session.message_id[:12],
                _CARD_CREATION_WAIT_SEC,
            )
            task.cancel()
            session.mark_failed()
            return False
        except asyncio.CancelledError:
            session.mark_failed()
            return False
        except Exception:
            _logger.debug("card creation task failed", exc_info=True)
            return False

    def _apply_completion_payload(
        self,
        *,
        session: CardSession,
        answer: str,
        duration: float,
        model: str,
        tokens: dict | None,
        context: dict | None,
    ) -> None:
        has_visible_answer = bool(
            session.segment_state
            and any(
                seg.type == SegmentType.ANSWER and seg.text.strip()
                for seg in session.segment_state.segments
            )
        )
        _logger.debug(
            "completion payload msg=%s len=%d has_visible_answer=%s final_started=%s head=%r",
            session.message_id[:12],
            len(answer or ""),
            has_visible_answer,
            session.interim_preview.final_started,
            (answer or "")[:160],
        )

        if answer and session.segment_state and not has_visible_answer:
            final_answer = strip_reasoning_tags(answer)
            if final_answer.strip():
                _logger.debug(
                    "completion promote_final msg=%s len=%d head=%r",
                    session.message_id[:12],
                    len(final_answer),
                    final_answer[:160],
                )
                self._start_final(session, source="completion")
                self._pause_merged_reasoning(session)
                self._append_answer_segment(session, final_answer)

        # 仅在 DeepSeek 模型下查询余额
        balance = ""
        if model and "deepseek" in model.lower():
            try:
                import subprocess
                from pathlib import Path

                result = subprocess.run(
                    ["bash", str(Path.home() / ".hermes/scripts/deepseek-balance.sh")],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    balance = f"¥{result.stdout.strip()}"
            except Exception:
                pass

        quota = _fetch_gpt_quota_footer(model)

        footer = dict(session.footer)
        footer.update({"duration": duration, "model": model, "balance": balance})
        if tokens:
            footer.update(_turn_usage_footer_data(tokens, api_calls=tokens.get("api_calls", 0)))
        if context:
            footer.update(
                {
                    "context_used": context.get("used_tokens"),
                    "context_max": context.get("max_tokens"),
                }
            )
        if quota.get("remaining"):
            footer["gpt_quota_remaining"] = quota["remaining"]
        if quota.get("reset_at") not in (None, ""):
            footer["gpt_quota_reset_at"] = quota["reset_at"]
        session.footer = footer

    def _complete_session(self, session: CardSession) -> None:
        """异步完成当前流式卡片."""
        session.flush.mark_completed()
        self._fire_and_forget(self._complete_session_after_creation(session), session._loop)

    async def _complete_session_after_creation(
        self,
        session: CardSession,
        *,
        require_card: bool = False,
    ) -> bool:
        if not await self._wait_for_card_creation(session):
            self._cleanup_session(session)
            return False
        if require_card and not session.card_id:
            _logger.info("card completion skipped: no active card msg=%s", session.message_id[:12])
            self._cleanup_session(session)
            return False
        return await self._complete_session_wait(session)

    async def _complete_session_wait(self, session: CardSession) -> bool:
        """完成当前流式卡片，并等待最终 API 结果."""
        session.progress.clear()
        session.flush.mark_completed()
        return await self._do_complete_card(session)

    def _prune_stale_sessions(self) -> None:
        now = time.time()
        stale = [mid for mid, s in self._sessions.items() if mid is not None and now - s.created_at > self._session_ttl]
        for mid in stale:
            _logger.warning("pruning stale session: msg=%s", mid[:12])
            self._cleanup(mid)

    @staticmethod
    def _on_bg_task_done(fut: asyncio.Future[Any] | ConcurrentFuture) -> None:
        try:
            fut.result()
        except asyncio.CancelledError:
            return
        except Exception:
            _logger.warning("background task failed", exc_info=True)


_controllers: dict[str, StreamCardController] = {}
_controller_lock = threading.Lock()


def get_controller(profile_home: Path | None = None) -> StreamCardController:
    resolved_home = (profile_home or hermes_home()).resolve()
    key = str(resolved_home)
    with _controller_lock:
        controller = _controllers.get(key)
        if controller is None:
            controller = StreamCardController(resolved_home)
            _controllers[key] = controller
        return controller
