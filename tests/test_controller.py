"""controller.py 测试 — 会话生命周期边界条件 + 流式卡片 dispatch 与集成测试."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from contextlib import nullcontext
from contextvars import ContextVar
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import hermes_lark_streaming.controller as controller_module
from hermes_lark_streaming.cardkit.builder import (
    _LOADING_ELEMENT_ID,
    REASONING_ELEMENT_ID,
    REASONING_TEXT_ELEMENT_ID,
    STREAMING_ELEMENT_ID,
    TOOL_PANEL_ELEMENT_ID,
    estimate_cardkit_elements,
)
from hermes_lark_streaming.controller import StreamCardController, get_controller
from hermes_lark_streaming.feishu import FeishuAPIError, FeishuClient
from hermes_lark_streaming.patch import on_reasoning_delta
from hermes_lark_streaming.streaming.presentation import project_tool_panel
from hermes_lark_streaming.streaming.segment_helper import ELEMENT_THRESHOLD, FOOTER_RESERVE, estimate_segment_elements
from hermes_lark_streaming.streaming.segments import Segment, SegmentState
from hermes_lark_streaming.streaming.session import CardSession, SessionState

_COMMENTARY_STAGE_1 = "第 1 阶段\uFF1A读取项目配置\uFF0C确认项目版本。"
_COMMENTARY_CONFIRMED_1 = "已确认第 1 项\uFF1Ahermes-lark-streaming 项目版本是 0.12.0。"
_COMMENTARY_STAGE_2 = "第 2 阶段\uFF1A继续读取 AGENTS.md\uFF0C确认最低 Hermes 版本。"
_FINAL_ANSWER = "第 3 阶段\uFF1A比较与总结\n项目版本……\n最低 Hermes 版本……\n最终结论……"


def _run_details_panel(card: dict) -> dict:
    return next(
        element
        for element in card["body"]["elements"]
        if element.get("tag") == "collapsible_panel"
        and "💭" not in element.get("header", {}).get("title", {}).get("content", "")
        and element.get("element_id") is None
    )


def _run_details_text(panel: dict) -> str:
    """Collect markdown from both ordinary and visual Run Details rows."""
    parts: list[str] = []

    def visit(element: dict) -> None:
        if element.get("tag") == "markdown":
            parts.append(element.get("content", ""))
        for column in element.get("columns", []):
            for child in column.get("elements", []):
                visit(child)

    for element in panel.get("elements", []):
        visit(element)
    return "\n".join(parts)


def _enable(ctrl: StreamCardController) -> None:
    ctrl._cfg._raw = {
        "streaming": {"enabled": True},
        "feishu": {"app_id": "app", "app_secret": "secret"},
    }
    _set_cached_loop(ctrl)


def test_get_controller_returns_one_instance_per_profile_home(tmp_path) -> None:
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()

    with patch.object(controller_module, "_controllers", {}), patch(
        "hermes_lark_streaming.controller.hermes_home",
        side_effect=[home_a, home_b, home_a],
    ):
        controller_a = get_controller()
        controller_b = get_controller()
        controller_a_again = get_controller()

    assert controller_a is controller_a_again
    assert controller_a is not controller_b
    assert controller_a._profile_home == home_a.resolve()
    assert controller_b._profile_home == home_b.resolve()


def test_enabled_caches_unscoped_fallback_result() -> None:
    ctrl = StreamCardController()
    ctrl._cfg = MagicMock()
    ctrl._cfg.enabled = True
    ctrl._cfg.feishu_app_id = "app-id"

    with patch.object(ctrl, "_needs_fallback_scope", return_value=True), patch.object(
        ctrl, "_credential_scope", side_effect=lambda: nullcontext()
    ) as credential_scope:
        assert ctrl.enabled is True
        assert ctrl.enabled is True

    credential_scope.assert_called_once()


def test_enabled_retries_unsuccessful_unscoped_fallback() -> None:
    ctrl = StreamCardController()
    ctrl._cfg = MagicMock()
    ctrl._cfg.enabled = True
    ctrl._cfg.feishu_app_id = ""
    ctrl._cfg.env_app_id = ""

    with patch.object(ctrl, "_needs_fallback_scope", return_value=True), patch.object(
        ctrl, "_credential_scope", side_effect=lambda: nullcontext()
    ) as credential_scope:
        assert ctrl.enabled is False
        ctrl._cfg.feishu_app_id = "app-id"
        assert ctrl.enabled is True

    assert credential_scope.call_count == 2


def test_gpt_quota_lookup_failure_is_fail_open() -> None:
    with patch.dict(sys.modules, {"agent.credential_pool": None}):
        assert controller_module._fetch_gpt_quota_footer("gpt-5-codex") == {}


def test_gpt_quota_source_separates_remaining_and_reset() -> None:
    credential_pool = ModuleType("agent.credential_pool")
    credential_pool.load_pool = MagicMock(  # type: ignore[attr-defined]
        return_value=SimpleNamespace(
            select=lambda: SimpleNamespace(
                access_token="token",
                base_url="https://chatgpt.com/backend-api/codex",
                extra={"account_id": "account"},
            )
        )
    )
    response = MagicMock()
    response.json.return_value = {
        "rate_limit": {
            "primary_window": {
                "limit_window_seconds": 18_000,
                "used_percent": 5,
                "reset_at": "2026-08-11T02:30:00+00:00",
            },
            "secondary_window": {
                "limit_window_seconds": 604800,
                "used_percent": 5,
                "reset_at": "2026-08-16T02:30:00+00:00",
            }
        }
    }

    with patch.dict(sys.modules, {"agent.credential_pool": credential_pool}), patch(
        "httpx.get", return_value=response
    ):
        quota = controller_module._fetch_gpt_quota_footer("gpt-5.6-luna")

    assert quota == {
        "remaining": "<font color='green'>95%</font>",
        "reset_at": "2026-08-16T02:30:00+00:00",
    }
    response.raise_for_status.assert_called_once_with()


def test_weekly_quota_window_can_be_primary_and_is_selected_by_duration() -> None:
    weekly = {"limit_window_seconds": 604800, "used_percent": 80, "reset_at": 123}
    short = {"limit_window_seconds": 18_000, "used_percent": 5, "reset_at": 456}
    assert controller_module._weekly_quota_window({"primary_window": weekly, "secondary_window": short}) is weekly


def test_missing_weekly_quota_window_is_fail_open() -> None:
    assert controller_module._weekly_quota_window(
        {"primary_window": {"limit_window_seconds": 18_000, "used_percent": 5}}
    ) is None


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (100, "green"),
        (50, "green"),
        (49, "orange"),
        (20, "orange"),
        (19, "red"),
        (0, "red"),
    ],
)
def test_quota_color_thresholds(remaining: int, expected: str) -> None:
    assert controller_module._quota_color(remaining) == expected


def test_current_turn_usage_maps_canonical_metadata_without_session_totals() -> None:
    data = controller_module._turn_usage_footer_data(
        {
            "prompt_tokens": 70_500,
            "input_tokens": 18_200,
            "output_tokens": 2_100,
            "cache_read_tokens": 52_300,
            "cache_write_tokens": 0,
            "reasoning_tokens": 1_600,
            "session_input_tokens": 999_999,
        },
        api_calls=3,
    )

    assert data == {
        "input_tokens": 70_500,
        "cache_prompt_tokens": 70_500,
        "output_tokens": 2_100,
        "cache_read_tokens": 52_300,
        "reasoning_tokens": 1_600,
        "api_calls": 3,
    }
    assert 999_999 not in data.values()


def test_on_turn_usage_stores_only_positive_current_turn_metadata() -> None:
    loop = asyncio.new_event_loop()
    try:
        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.enabled = True
        ctrl._cfg.feishu_app_id = "app"
        ctrl._cfg.env_app_id = ""
        session = CardSession("usage-message", "chat", loop)
        ctrl._sessions[session.message_id] = session

        ctrl.on_turn_usage(
            message_id=session.message_id,
            usage={
                "prompt_tokens": 1_200,
                "output_tokens": 0,
                "cache_read_tokens": 200,
                "reasoning_tokens": None,
            },
        )

        assert session.footer == {
            "input_tokens": 1_200,
            "cache_prompt_tokens": 1_200,
            "cache_read_tokens": 200,
        }
    finally:
        loop.close()


def test_reasoning_hook_forwards_api_mode() -> None:
    ctrl = MagicMock()
    ctrl.enabled = True
    ctrl.on_reasoning.return_value = True

    with patch("hermes_lark_streaming.patch.get_controller", return_value=ctrl):
        assert on_reasoning_delta(
            message_id="msg",
            text="Planning",
            api_mode="codex_responses",
        ) is True

    ctrl.on_reasoning.assert_called_once_with(
        message_id="msg",
        text="Planning",
        api_mode="codex_responses",
    )


def test_enabled_uses_and_restores_profile_secret_scope(tmp_path, monkeypatch) -> None:
    secret_scope = ModuleType("agent.secret_scope")
    active_scope: ContextVar[dict[str, str] | None] = ContextVar("active_scope", default=None)
    scoped_homes = []

    def build_profile_secret_scope(home) -> dict[str, str]:
        scoped_homes.append(home)
        return {"FEISHU_APP_ID": "profile-app", "FEISHU_APP_SECRET": "profile-secret"}

    def get_secret(name: str, default: str = "") -> str:
        return (active_scope.get() or {}).get(name, default)

    secret_scope.build_profile_secret_scope = build_profile_secret_scope
    secret_scope.current_secret_scope = active_scope.get
    secret_scope.get_secret = get_secret
    secret_scope.is_multiplex_active = lambda: True
    secret_scope.reset_secret_scope = active_scope.reset
    secret_scope.set_secret_scope = active_scope.set
    agent = ModuleType("agent")
    agent.secret_scope = secret_scope
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text("streaming:\n  enabled: true\n", encoding="utf-8")
    ctrl = StreamCardController(profile_home)
    assert active_scope.get() is None
    assert ctrl.enabled is True
    assert active_scope.get() is None
    assert scoped_homes == [profile_home]


def _set_cached_loop(ctrl: StreamCardController) -> asyncio.AbstractEventLoop:
    """为同步测试预置缓存的 event loop。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ctrl._loop = loop
    return loop


class _DummyFlush:
    def __init__(self) -> None:
        self.completed = False

    def mark_completed(self) -> None:
        self.completed = True


@pytest.mark.parametrize("message_id", [None, ""])
def test_on_message_started_ignores_missing_message_id(message_id: str | None) -> None:
    ctrl = StreamCardController()
    _enable(ctrl)

    ctrl.on_message_started(message_id=message_id, chat_id="chat")

    assert ctrl._sessions == {}


def test_on_message_started_registers_anchor_alias_and_cleanup() -> None:
    ctrl = StreamCardController()
    _enable(ctrl)

    with patch.object(ctrl, "_fire_and_forget", side_effect=lambda coro, loop: coro.close()):
        ctrl.on_message_started(message_id="msg", chat_id="chat", anchor_id="quoted")

    session = ctrl._sessions["msg"]
    assert ctrl._sessions["quoted"] is session
    assert session.anchor_id == "quoted"

    ctrl._cleanup("msg")

    assert "msg" not in ctrl._sessions
    assert "quoted" not in ctrl._sessions


def test_consume_text_fallback_clears_anchor_alias() -> None:
    ctrl = StreamCardController()
    session = _make_session("msg")
    session.anchor_id = "quoted"
    ctrl._sessions["msg"] = session
    ctrl._sessions["quoted"] = session
    ctrl._mark_text_fallback_needed(session)
    ctrl._cleanup("msg")

    assert ctrl.consume_text_fallback("msg") is True

    assert "msg" not in ctrl._text_fallback_needed
    assert "quoted" not in ctrl._text_fallback_needed
    assert ctrl._text_fallback_aliases == {}


def test_on_interrupted_uses_new_message_id_and_anchor_alias() -> None:
    ctrl = StreamCardController()
    _enable(ctrl)

    with patch.object(ctrl, "_fire_and_forget", side_effect=lambda coro, loop: coro.close()):
        ctrl.on_message_started(message_id="old", chat_id="chat", session_key="session:chat")
        ctrl.on_interrupted(
            old_message_id="old",
            new_message_id="new",
            chat_id="chat",
            anchor_id="quoted",
        )

    session = ctrl._sessions["new"]
    assert ctrl._sessions["quoted"] is session
    assert session.anchor_id == "quoted"
    assert ctrl._interrupt_map["old"] == "new"
    assert ctrl._sessions["old"].state == SessionState.ABORTED
    assert session.session_key == "session:chat"
    assert ctrl._session_keys["session:chat"] is session


def test_on_interrupted_same_id_replaces_terminal_session() -> None:
    ctrl = StreamCardController()
    _enable(ctrl)

    with patch.object(ctrl, "_fire_and_forget", side_effect=lambda coro, loop: coro.close()):
        ctrl.on_message_started(message_id="msg", chat_id="chat")
        old_session = ctrl._sessions["msg"]
        ctrl.on_interrupted(
            old_message_id="msg",
            new_message_id="msg",
            chat_id="chat",
            anchor_id="msg",
        )

    new_session = ctrl._sessions["msg"]
    assert old_session.state == SessionState.ABORTED
    assert new_session is not old_session
    assert new_session.state == SessionState.IDLE

    ctrl._cleanup_session(old_session)

    assert ctrl._sessions["msg"] is new_session


@pytest.mark.asyncio
async def test_on_session_aborted_only_stops_matching_session_key() -> None:
    ctrl = StreamCardController()
    _enable(ctrl)

    with patch.object(ctrl, "_fire_and_forget", side_effect=lambda coro, loop: coro.close()):
        ctrl.on_message_started(
            message_id="first",
            chat_id="shared-chat",
            session_key="session:first",
        )
        ctrl.on_message_started(
            message_id="second",
            chat_id="shared-chat",
            session_key="session:second",
        )
        ctrl._sessions["first"].card_id = "card:first"
        with patch.object(ctrl, "_complete_session_wait", new_callable=AsyncMock, return_value=True) as complete:
            assert await ctrl.on_session_aborted(session_key="session:first") is True

    assert ctrl._sessions["first"].state == SessionState.ABORTED
    assert ctrl._sessions["second"].state == SessionState.IDLE
    assert "session:first" not in ctrl._session_keys
    assert ctrl._session_keys["session:second"] is ctrl._sessions["second"]
    complete.assert_awaited_once_with(ctrl._sessions["first"])


@pytest.mark.asyncio
async def test_on_session_aborted_waits_for_card_creation() -> None:
    ctrl = _setup_ctrl()
    session = CardSession("creating", "chat", asyncio.get_running_loop())
    session.session_key = "session:creating"
    ctrl._sessions["creating"] = session
    ctrl._session_keys["session:creating"] = session
    ready = asyncio.Event()

    async def finish_create() -> None:
        session.state = SessionState.CREATING
        await ready.wait()
        session.card_id = "card"
        session.card_msg_id = "card-message"

    session.create_task = asyncio.create_task(finish_create())

    with patch.object(ctrl, "_complete_session_wait", new_callable=AsyncMock, return_value=True) as complete:
        waiter = asyncio.create_task(ctrl.on_session_aborted(session_key="session:creating"))
        await asyncio.sleep(0.01)
        assert not waiter.done()
        assert session.state == SessionState.ABORTED

        ready.set()
        assert await waiter is True

    complete.assert_awaited_once_with(session)


@pytest.mark.asyncio
async def test_stop_session_aborted_sets_continue_hint_only_for_explicit_stop() -> None:
    ctrl = _setup_ctrl()
    stop_session = _make_session("stop")
    stop_session.session_key = "session:stop"
    stop_session.card_id = "card:stop"
    ctrl._sessions["stop"] = stop_session
    ctrl._session_keys["session:stop"] = stop_session

    generic_session = _make_session("generic")
    generic_session.session_key = "session:generic"
    generic_session.card_id = "card:generic"
    ctrl._sessions["generic"] = generic_session
    ctrl._session_keys["session:generic"] = generic_session

    with patch.object(ctrl, "_complete_session_wait", new_callable=AsyncMock, return_value=True):
        assert await ctrl.on_session_aborted(session_key="session:stop", stop_command=True) is True
        assert await ctrl.on_session_aborted(session_key="session:generic") is True

    assert stop_session.footer["stop_continue_hint"] is True
    assert "stop_continue_hint" not in generic_session.footer


@pytest.mark.asyncio
async def test_stop_session_aborted_fails_open_without_card_or_on_update_failure() -> None:
    ctrl = _setup_ctrl()
    no_card = _make_session("no-card")
    no_card.session_key = "session:no-card"
    ctrl._sessions["no-card"] = no_card
    ctrl._session_keys["session:no-card"] = no_card

    with patch.object(ctrl, "_complete_session_wait", new_callable=AsyncMock, return_value=True) as complete:
        assert await ctrl.on_session_aborted(session_key="session:no-card", stop_command=True) is False
    complete.assert_not_awaited()

    failed = _make_session("failed")
    failed.session_key = "session:failed"
    failed.card_id = "card:failed"
    ctrl._sessions["failed"] = failed
    ctrl._session_keys["session:failed"] = failed
    with patch.object(ctrl, "_complete_session_wait", new_callable=AsyncMock, return_value=False):
        assert await ctrl.on_session_aborted(session_key="session:failed", stop_command=True) is False


@pytest.mark.asyncio
async def test_on_interrupted_waits_for_old_card_creation_before_cleanup() -> None:
    ctrl = _setup_ctrl()
    old_session = CardSession("old", "chat", asyncio.get_running_loop())
    ctrl._sessions["old"] = old_session
    ready = asyncio.Event()
    completed: list[str] = []
    tasks: list[asyncio.Task[object]] = []

    async def finish_create() -> None:
        old_session.state = SessionState.CREATING
        await ready.wait()
        old_session.card_id = "old-card"
        old_session.card_msg_id = "old-card-message"

    async def complete_card(_session: CardSession) -> bool:
        completed.append("old")
        return True

    def schedule(coro: object, _loop: asyncio.AbstractEventLoop) -> asyncio.Task[object]:
        task = asyncio.create_task(coro)  # type: ignore[arg-type]
        tasks.append(task)
        return task

    old_session.create_task = asyncio.create_task(finish_create())
    with (
        patch.object(ctrl, "_do_complete_card_inner", side_effect=complete_card),
        patch.object(ctrl, "_do_create_card", new_callable=AsyncMock),
        patch.object(ctrl, "_fire_and_forget", side_effect=schedule),
    ):
        ctrl.on_interrupted(
            old_message_id="old",
            new_message_id="new",
            chat_id="chat",
        )
        await asyncio.sleep(0)

        assert "old" in ctrl._sessions
        assert completed == []

        ready.set()
        await asyncio.gather(old_session.create_task, *tasks)

    assert completed == ["old"]
    assert "old" not in ctrl._sessions


def test_prune_stale_sessions_ignores_none_key_and_prunes_valid_key() -> None:
    ctrl = StreamCardController()
    stale_session = SimpleNamespace(
        created_at=time.time() - ctrl._session_ttl - 1,
        flush=_DummyFlush(),
        image_resolver=None,
    )
    valid_stale_session = SimpleNamespace(
        created_at=time.time() - ctrl._session_ttl - 1,
        flush=_DummyFlush(),
        image_resolver=None,
    )
    ctrl._sessions[None] = stale_session  # type: ignore[index,assignment]
    ctrl._sessions["msg"] = valid_stale_session  # type: ignore[assignment]

    ctrl._prune_stale_sessions()

    assert ctrl._sessions[None] is stale_session  # type: ignore[index]
    assert "msg" not in ctrl._sessions
    assert valid_stale_session.flush.completed


@pytest.mark.asyncio
async def test_background_review_deferred_until_complete() -> None:
    ctrl = _setup_ctrl()
    session = _make_session("msg_bg")
    session.state = SessionState.STREAMING
    session.card_msg_id = "card_msg"
    ctrl._sessions["msg_bg"] = session
    sent: list[str] = []

    assert ctrl.defer_background_review(message_id="msg_bg", text="review", sender=sent.append)
    assert sent == []

    with patch.object(ctrl, "_do_complete_card_inner", new_callable=AsyncMock, return_value=True):
        await ctrl._do_complete_card(session)

    assert sent == ["review"]
    assert "msg_bg" not in ctrl._sessions


@pytest.mark.asyncio
async def test_background_review_does_not_interrupt_replacement_card() -> None:
    ctrl = _setup_ctrl()
    old_session = CardSession("old", "chat", asyncio.get_running_loop())
    old_session.state = SessionState.STREAMING
    old_session.card_id = "old-card"
    old_session.card_msg_id = "old-card-message"
    ctrl._sessions["old"] = old_session
    events: list[str] = []
    tasks: list[asyncio.Task[object]] = []

    assert ctrl.defer_background_review(
        message_id="old",
        text="review",
        sender=lambda _text: events.append("review_sent"),
    )

    async def complete_card(_session: CardSession) -> bool:
        events.append("old_card_completed")
        return True

    def schedule(coro: object, _loop: asyncio.AbstractEventLoop) -> asyncio.Task[object]:
        task = asyncio.create_task(coro)  # type: ignore[arg-type]
        tasks.append(task)
        return task

    with (
        patch.object(ctrl, "_do_complete_card_inner", side_effect=complete_card),
        patch.object(ctrl, "_do_create_card", new_callable=AsyncMock),
        patch.object(ctrl, "_fire_and_forget", side_effect=schedule),
    ):
        ctrl.on_interrupted(
            old_message_id="old",
            new_message_id="new",
            chat_id="chat",
        )
        await asyncio.gather(*tasks)

    assert events == ["old_card_completed", "review_sent"]
    assert "old" not in ctrl._sessions
    assert ctrl._sessions["new"].deferred_background_reviews == []


def test_background_review_without_active_session_not_deferred() -> None:
    ctrl = _setup_ctrl()
    sent: list[str] = []

    assert not ctrl.defer_background_review(message_id="missing", text="review", sender=sent.append)
    assert sent == []


def test_background_review_after_flush_not_deferred() -> None:
    ctrl = _setup_ctrl()
    session = _make_session("msg_bg")
    ctrl._sessions["msg_bg"] = session
    sent: list[str] = []

    ctrl._flush_deferred_background_reviews(session)

    assert not ctrl.defer_background_review(message_id="msg_bg", text="review", sender=sent.append)
    assert sent == []


# ── 辅助函数 ──


def _make_session(msg_id: str = "msg_123") -> CardSession:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return CardSession(msg_id, "chat_456", loop)


def _mock_client() -> AsyncMock:
    client = AsyncMock(spec=FeishuClient)
    client.cardkit_create = AsyncMock(return_value="card_id_abc")
    client.reply_card_by_id = AsyncMock(return_value="msg_id_reply")
    client.cardkit_batch_update = AsyncMock()
    client.cardkit_stream_element = AsyncMock()
    client.cardkit_close_streaming = AsyncMock()
    client.cardkit_update = AsyncMock()
    return client


def _setup_ctrl() -> StreamCardController:
    ctrl = StreamCardController()
    _enable(ctrl)
    ctrl._initialized = True
    ctrl._client = _mock_client()
    return ctrl


def _configure_merged(
    ctrl: StreamCardController,
    *,
    show_reasoning: bool = True,
    show_tool_use: bool = True,
    show_tool_detail: bool = True,
    tool_detail_mode: str = "full",
) -> None:
    ctrl._cfg._raw["streaming"]["reasoning_mode"] = "merged"
    ctrl._cfg._reload = lambda: {  # type: ignore[assignment]
        "display": {
            "platforms": {
                "feishu": {
                    "show_reasoning": show_reasoning,
                    "show_tool_use": show_tool_use,
                    "show_tool_detail": show_tool_detail,
                    "tool_detail_mode": tool_detail_mode,
                }
            }
        }
    }


def _configure_progress(
    ctrl: StreamCardController,
    *,
    show_reasoning: bool = True,
    show_tool_use: bool = True,
    show_tool_detail: bool = True,
    tool_detail_mode: str = "full",
) -> None:
    ctrl._cfg._raw["streaming"]["progress_mode"] = "card"
    ctrl._cfg._reload = lambda: {  # type: ignore[assignment]
        "display": {
            "platforms": {
                "feishu": {
                    "show_reasoning": show_reasoning,
                    "show_tool_use": show_tool_use,
                    "show_tool_detail": show_tool_detail,
                    "tool_detail_mode": tool_detail_mode,
                }
            }
        }
    }


class TestAwaitedCompletion:
    @pytest.mark.asyncio
    async def test_waits_for_queued_card_creation_before_success(self) -> None:
        ctrl = _setup_ctrl()
        session = CardSession("msg_wait", "chat", asyncio.get_running_loop())
        ctrl._sessions["msg_wait"] = session
        ready = asyncio.Event()

        async def finish_create() -> None:
            await ready.wait()
            session.card_id = "card_wait"
            session.card_msg_id = "card_msg_wait"
            session.state = SessionState.STREAMING

        session.create_task = asyncio.create_task(finish_create())

        with patch.object(ctrl, "_complete_session_wait", new_callable=AsyncMock, return_value=True) as complete:
            waiter = asyncio.create_task(ctrl.on_completed_wait(message_id="msg_wait", answer="ok"))
            await asyncio.sleep(0.01)
            assert not waiter.done()

            ready.set()

            assert await waiter is True
            complete.assert_awaited_once_with(session)

    @pytest.mark.asyncio
    async def test_card_creation_timeout_yields_to_gateway(self) -> None:
        ctrl = _setup_ctrl()
        session = CardSession("msg_timeout", "chat", asyncio.get_running_loop())
        ctrl._sessions["msg_timeout"] = session
        session.create_task = asyncio.create_task(asyncio.sleep(60))

        with patch("hermes_lark_streaming.controller._CARD_CREATION_WAIT_SEC", 0.01):
            assert await ctrl.on_completed_wait(message_id="msg_timeout", answer="ok") is False

        assert session.state == SessionState.FAILED
        assert session.progress.visible is False
        assert "msg_timeout" not in ctrl._sessions

    @pytest.mark.asyncio
    async def test_finalization_failure_yields_to_gateway(self) -> None:
        ctrl = _setup_ctrl()
        session = CardSession("msg_fail", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_fail"
        session.card_msg_id = "card_msg_fail"
        ctrl._sessions["msg_fail"] = session

        with patch.object(ctrl, "_complete_session_wait", new_callable=AsyncMock, return_value=False):
            assert await ctrl.on_completed_wait(message_id="msg_fail", answer="ok") is False

    @pytest.mark.asyncio
    async def test_short_reply_adds_final_answer_segment(self) -> None:
        ctrl = _setup_ctrl()
        session = CardSession("msg_short", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.segment_state = SegmentState()
        session.card_id = "card_short"
        session.card_msg_id = "card_msg_short"
        ctrl._sessions["msg_short"] = session

        with patch.object(ctrl, "_complete_session_wait", new_callable=AsyncMock, return_value=True):
            assert await ctrl.on_completed_wait(message_id="msg_short", answer="short") is True

        assert len(session.segment_state.segments) == 1
        assert session.segment_state.segments[0].type == "answer"
        assert session.segment_state.segments[0].text == "short"

    @pytest.mark.asyncio
    async def test_agent_failure_finalizes_card_as_error(self) -> None:
        ctrl = _setup_ctrl()
        session = CardSession("msg_error", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_error"
        session.card_msg_id = "card_msg_error"
        ctrl._sessions["msg_error"] = session

        with patch.object(ctrl, "_complete_session_wait", new_callable=AsyncMock, return_value=True):
            assert await ctrl.on_completed_wait(
                message_id="msg_error", answer="request failed", is_error=True,
            ) is True

        assert session.state == SessionState.FAILED

    @pytest.mark.asyncio
    async def test_completion_merges_turn_usage_with_structured_quota(self) -> None:
        ctrl = _setup_ctrl()
        session = CardSession("msg_usage_complete", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_usage_complete"
        session.card_msg_id = "card_msg_usage_complete"
        session.footer = {
            "input_tokens": 12_400,
            "output_tokens": 1_800,
            "cache_read_tokens": 10_200,
            "reasoning_tokens": 3_600,
        }
        ctrl._sessions[session.message_id] = session

        with patch.object(
            ctrl, "_complete_session_wait", new_callable=AsyncMock, return_value=True
        ), patch(
            "hermes_lark_streaming.controller._fetch_gpt_quota_footer",
            return_value={
                "remaining": "<font color='green'>95%</font>",
                "reset_at": "2026-08-16T02:30:00+00:00",
            },
        ):
            assert await ctrl.on_completed_wait(
                message_id=session.message_id,
                answer="answer",
                duration=4.4,
                model="gpt-5.6-luna",
                context={"used_tokens": 70_500, "max_tokens": 272_000},
            ) is True

        assert session.footer == {
            "duration": 4.4,
            "model": "gpt-5.6-luna",
            "input_tokens": 12_400,
            "output_tokens": 1_800,
            "cache_read_tokens": 10_200,
            "reasoning_tokens": 3_600,
            "context_used": 70_500,
            "context_max": 272_000,
            "balance": "",
            "gpt_quota_remaining": "<font color='green'>95%</font>",
            "gpt_quota_reset_at": "2026-08-16T02:30:00+00:00",
        }


@pytest.mark.asyncio
async def test_create_card_replies_to_anchor_id() -> None:
    ctrl = _setup_ctrl()
    session = _make_session("msg")
    session.anchor_id = "quoted"

    await ctrl._do_create_card(session)

    ctrl._client.reply_card_by_id.assert_called_once()
    assert ctrl._client.reply_card_by_id.call_args.args[0] == "quoted"


def _capture_split_calls(
    ctrl: StreamCardController,
    *,
    cards: list[str] | None = None,
    messages: list[str] | None = None,
    create_error: Exception | None = None,
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    client = ctrl._client
    card_iter = iter(cards or ["card_next"])
    message_iter = iter(messages or ["msg_next"])

    client.cardkit_batch_update = AsyncMock(
        side_effect=lambda card_id, *a, **k: calls.append(("batch", card_id))
    )
    if create_error is None:
        client.cardkit_create = AsyncMock(
            side_effect=lambda *a, **k: calls.append(("create", "")) or next(card_iter)
        )
    else:
        client.cardkit_create = AsyncMock(side_effect=create_error)
    client.reply_card_by_id = AsyncMock(
        side_effect=lambda *a, **k: calls.append(("reply", "")) or next(message_iter)
    )
    client.cardkit_close_streaming = AsyncMock(
        side_effect=lambda card_id, **k: calls.append(("close", card_id))
    )
    client.cardkit_update = AsyncMock(
        side_effect=lambda card_id, *a, **k: calls.append(("seal", card_id))
    )
    return calls


# ── Dispatch 测试 — 流式卡片分流 ──


class TestDispatch:
    """验证流式卡片 session 的入口会消费事件并更新 SegmentState."""

    @pytest.mark.parametrize("event,kwargs,seg_type", [
        ("on_reasoning", {"text": "r"}, "reasoning"),
        ("on_answer", {"text": "a"}, "answer"),
    ])
    def test_dispatch_creates_segment(self, event: str, kwargs: dict, seg_type: str) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._reload = lambda: {"display": {"platforms": {"feishu": {"show_reasoning": True}}}}  # type: ignore[assignment]
        session = _make_session("msg_d")
        ctrl._sessions["msg_d"] = session
        assert getattr(ctrl, event)(message_id="msg_d", **kwargs) is True
        assert session.segment_state.segments[0].type == seg_type

    def test_thinking_dispatches(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_t")
        ctrl._sessions["msg_t"] = session
        with patch.object(ctrl, "_on_thinking_segment") as m:
            m.return_value = True
            ctrl.on_thinking(message_id="msg_t", text="thinking")
            m.assert_called_once()

    def test_tool_dispatches(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_tool")
        ctrl._sessions["msg_tool"] = session
        assert ctrl.on_tool_update(message_id="msg_tool", tool_name="read", status="started") is True
        assert session.segment_state.segments[0].type == "tool"

    def test_session_without_segment_state_not_consumed(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_no_state")
        session.segment_state = None
        ctrl._sessions["msg_no_state"] = session
        assert ctrl.on_answer(message_id="msg_no_state", text="answer text") is False
        assert session.segment_state is None

    def test_message_started_consumes_delta_before_create_task_runs(self) -> None:
        ctrl = _setup_ctrl()

        with patch.object(ctrl, "_fire_and_forget", side_effect=lambda coro, loop: coro.close()):
            ctrl.on_message_started(message_id="msg_early_delta", chat_id="chat1")

        session = ctrl._sessions["msg_early_delta"]
        assert session.segment_state is not None
        assert ctrl.on_answer(message_id="msg_early_delta", text="early") is True
        assert session.segment_state.segments[0].text == "early"

    def test_guard_skips_terminal(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_term")
        session.state = SessionState.COMPLETED
        ctrl._sessions["msg_term"] = session
        assert ctrl.on_answer(message_id="msg_term", text="late text") is False
        assert len(session.segment_state.segments) == 0

    def test_message_started_creates_session(self) -> None:
        ctrl = _setup_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        session = ctrl._sessions["msg1"]
        loop = session._loop
        loop.run_until_complete(asyncio.sleep(0.05))
        assert session.segment_state is not None
        assert session.card_id is not None


# ── _do_create_card 集成测试 ──


class TestDoCreateCard:
    @pytest.mark.asyncio
    async def test_streaming_card_starts_with_native_loading_element(self) -> None:
        ctrl = _setup_ctrl()
        _configure_progress(ctrl)
        session = _make_session("msg_progress_create")
        ctrl._sessions[session.message_id] = session

        await ctrl._do_create_card(session)

        card = ctrl._client.cardkit_create.await_args.args[0]
        loading = card["body"]["elements"][-1]
        assert loading["element_id"] == _LOADING_ELEMENT_ID
        assert loading["content"] == " "
        assert session.progress.visible is False
        assert session.progress.dirty is False

    @pytest.mark.asyncio
    async def test_card_mode_heartbeat_updates_loading_element_in_place(self) -> None:
        ctrl = _setup_ctrl()
        _configure_progress(ctrl)
        session = CardSession("msg_heartbeat", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_heartbeat"
        session.card_msg_id = "card_msg_heartbeat"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session

        assert ctrl.on_long_running_progress(
            message_id=session.message_id,
            elapsed_seconds=180,
            iteration=3,
            max_iterations=60,
        ) is True
        await ctrl._do_flush(session)

        progress_action = next(
            action
            for call in ctrl._client.cardkit_batch_update.await_args_list
            for action in call.args[1]
            if action.get("action") == "partial_update_element"
        )
        assert progress_action["params"]["element_id"] == _LOADING_ELEMENT_ID
        assert progress_action["params"]["partial_element"] == {
            "content": "Working · 3 min · Round 3",
            "i18n_content": {
                "en_us": "Working · 3 min · Round 3",
                "zh_cn": "运行中 · 3 分钟 · 第 3 轮",
            },
        }

        pending = CardSession("msg_pending_heartbeat", "chat", asyncio.get_running_loop())
        pending.state = SessionState.CREATING
        pending.card_id = "card_pending_heartbeat"
        ctrl._sessions[pending.message_id] = pending
        assert ctrl.on_long_running_progress(
            message_id=pending.message_id,
            elapsed_seconds=180,
        ) is False

    @pytest.mark.asyncio
    async def test_heartbeat_updates_only_the_mapped_active_card(self) -> None:
        ctrl = _setup_ctrl()
        _configure_progress(ctrl)
        first = CardSession("msg_heartbeat_one", "chat", asyncio.get_running_loop())
        first.state = SessionState.STREAMING
        first.card_id = "card_heartbeat_one"
        second = CardSession("msg_heartbeat_two", "chat", asyncio.get_running_loop())
        second.state = SessionState.STREAMING
        second.card_id = "card_heartbeat_two"
        ctrl._sessions[first.message_id] = first
        ctrl._sessions[second.message_id] = second

        assert ctrl.on_long_running_progress(
            message_id=first.message_id,
            elapsed_seconds=180,
        ) is True
        await ctrl._do_flush(first)

        assert ctrl._client.cardkit_batch_update.await_args.args[0] == first.card_id
        assert second.progress.visible is False

    @pytest.mark.asyncio
    async def test_reasoning_tool_answer_do_not_touch_heartbeat_state(self) -> None:
        ctrl = _setup_ctrl()
        _configure_progress(ctrl, show_tool_use=False)
        session = _make_session("msg_no_lifecycle_progress")
        session.state = SessionState.STREAMING
        session.card_id = "card_no_lifecycle_progress"
        ctrl._sessions[session.message_id] = session
        initial = session.progress.snapshot()

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_reasoning(message_id=session.message_id, text="plan") is True
            assert ctrl.on_tool_update(
                message_id=session.message_id,
                tool_name="read",
                status="started",
            ) is True
            assert ctrl.on_answer(message_id=session.message_id, text="answer") is True

        assert session.progress.snapshot() == initial
        assert session.progress.visible is False

    @pytest.mark.asyncio
    async def test_split_card_preserves_latest_heartbeat_on_native_loading(self) -> None:
        ctrl = _setup_ctrl()
        _configure_progress(ctrl)
        session = CardSession("msg_progress_split", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_progress_old"
        session.card_msg_id = "card_msg_progress_old"
        session.progress.note_heartbeat(360, iteration=8, max_iterations=60)

        assert await ctrl._do_split_card(session, 0, [], set(), {}, []) is True

        card = ctrl._client.cardkit_create.await_args.args[0]
        loading = card["body"]["elements"][-1]
        assert loading["element_id"] == _LOADING_ELEMENT_ID
        assert loading["content"] == "Working · 6 min · Round 8"
        assert loading["icon"]["img_key"] == "img_v3_02vb_496bec09-4b43-4773-ad6b-0cdd103cd2bg"

    @pytest.mark.asyncio
    async def test_cardkit_success(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_create")
        ctrl._sessions["msg_create"] = session

        await ctrl._do_create_card(session)

        assert session.segment_state is not None
        assert session.card_id == "card_id_abc"
        assert session.state == SessionState.STREAMING

    @pytest.mark.asyncio
    async def test_progress_update_failure_restores_future_heartbeat_fallback(self) -> None:
        ctrl = _setup_ctrl()
        _configure_progress(ctrl)
        session = CardSession("msg_progress_failure", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_progress_failure"
        session.card_msg_id = "card_msg_progress_failure"
        ctrl._sessions[session.message_id] = session
        ctrl._client.cardkit_batch_update = AsyncMock(side_effect=RuntimeError("update failed"))

        assert ctrl.on_long_running_progress(
            message_id=session.message_id,
            elapsed_seconds=180,
        ) is True

        await ctrl._flush_progress(session)

        assert session.progress.available is False
        assert ctrl.on_long_running_progress(
            message_id=session.message_id,
            elapsed_seconds=180,
        ) is False

    def test_failed_and_aborted_sessions_clear_progress(self) -> None:
        ctrl = _setup_ctrl()
        _configure_progress(ctrl)

        failed = _make_session("msg_progress_failed")
        failed.mark_failed()
        assert failed.progress.visible is False

        aborted = _make_session("msg_progress_aborted")
        aborted.state = SessionState.STREAMING
        aborted.card_id = "card_progress_aborted"
        ctrl._sessions[aborted.message_id] = aborted
        with patch.object(ctrl, "_complete_session"):
            ctrl.on_aborted(message_id=aborted.message_id)
        assert aborted.progress.visible is False
        assert aborted.state == SessionState.ABORTED

    @pytest.mark.asyncio
    async def test_completion_removes_progress_without_regressing_footer(self) -> None:
        ctrl = _setup_ctrl()
        _configure_progress(ctrl, show_tool_use=False)
        ctrl._cfg._raw["streaming"]["reasoning_mode"] = "merged"
        ctrl._cfg._raw["streaming"]["footer"] = {
            "fields": [["status", "elapsed", "context", "gpt_quota", "model"]]
        }
        session = CardSession("msg_progress_complete", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_progress_complete"
        session.card_msg_id = "card_msg_progress_complete"
        session.element_count = 2
        session.footer = {
            "duration": 26.5,
            "model": "gpt-5",
            "context_used": 50_000,
            "context_max": 200_000,
            "gpt_quota": "5h 80%",
        }
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_reasoning(
                message_id=session.message_id,
                text="reasoning",
                api_mode="codex_responses",
            ) is True
            assert ctrl.on_thinking(
                message_id=session.message_id,
                text="commentary",
                api_mode="codex_responses",
                source="interim_commentary",
            ) is True
            assert ctrl.on_answer(message_id=session.message_id, text="final answer") is True

        assert [seg.text for seg in session.segment_state.segments if seg.type == "answer"] == [
            "final answer"
        ]
        assert await ctrl._do_complete_card(session) is True
        assert session.progress.visible is False

        complete_card = ctrl._client.cardkit_update.await_args.args[1]
        assert complete_card["body"]["elements"]
        assert not any(
            element.get("element_id") == _LOADING_ELEMENT_ID
            for element in complete_card["body"]["elements"]
        )
        body_text = "\n".join(
            element.get("content", "")
            for element in complete_card["body"]["elements"]
            if element.get("tag") == "markdown"
        )
        assert "final answer" in body_text
        assert "commentary" not in body_text
        details = _run_details_panel(complete_card)
        assert details["expanded"] is False
        details_text = _run_details_text(details)
        assert "Context used 25%" in details_text

    @pytest.mark.asyncio
    async def test_applies_width_mode_to_streaming_card(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._raw["streaming"]["width_mode"] = "compact"
        session = _make_session("msg_width_create")

        await ctrl._do_create_card(session)

        card = ctrl._client.cardkit_create.await_args.args[0]
        assert card["config"]["width_mode"] == "compact"

    @pytest.mark.asyncio
    async def test_content_failure_recreates_card_and_retries_reply(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._client.cardkit_create = AsyncMock(side_effect=["card_first", "card_second"])
        ctrl._client.reply_card_by_id = AsyncMock(
            side_effect=[FeishuAPIError("invalid card", code=230099), "reply_second"]
        )
        session = _make_session("msg_retry")

        await ctrl._do_create_card(session)

        assert session.card_id == "card_second"
        assert session.card_msg_id == "reply_second"
        assert ctrl._client.cardkit_create.await_count == 2
        assert ctrl._client.reply_card_by_id.await_count == 2

    @pytest.mark.asyncio
    async def test_content_failure_uses_standalone_card_after_retry_fails(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._client.cardkit_create = AsyncMock(side_effect=["card_first", "card_second"])
        ctrl._client.reply_card_by_id = AsyncMock(side_effect=FeishuAPIError("invalid card", code=230099))
        ctrl._client.send_card_to_chat = AsyncMock(return_value="standalone_reply")
        session = _make_session("msg_standalone")

        await ctrl._do_create_card(session)

        assert session.card_id == "card_second"
        assert session.card_msg_id == "standalone_reply"
        ctrl._client.send_card_to_chat.assert_awaited_once_with(
            chat_id="chat_456",
            card={"type": "card", "data": {"card_id": "card_second"}},
        )

    @pytest.mark.asyncio
    async def test_standalone_card_completion_does_not_require_text_fallback(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._client.cardkit_create = AsyncMock(side_effect=["card_first", "card_second"])
        ctrl._client.reply_card_by_id = AsyncMock(side_effect=FeishuAPIError("invalid card", code=230099))
        ctrl._client.send_card_to_chat = AsyncMock(return_value="standalone_reply")
        session = _make_session("msg_standalone_complete")
        ctrl._sessions[session.message_id] = session

        await ctrl._do_create_card(session)

        with patch.object(ctrl, "_complete_session_wait", new_callable=AsyncMock, return_value=True):
            assert await ctrl.on_completed_wait(message_id=session.message_id, answer="answer") is True

        assert ctrl.consume_text_fallback(session.message_id) is False

    @pytest.mark.asyncio
    async def test_cardkit_failure_yields_to_gateway(self) -> None:
        ctrl = _setup_ctrl()
        client = ctrl._client
        client.cardkit_create = AsyncMock(side_effect=FeishuAPIError("fail", code=230099))
        session = _make_session("msg_fallback")
        ctrl._sessions["msg_fallback"] = session

        await ctrl._do_create_card(session)

        assert session.segment_state is not None
        assert session.state == SessionState.FAILED
        assert await ctrl.on_completed_wait(message_id="msg_fallback", answer="plain") is False
        assert ctrl.consume_text_fallback("msg_fallback") is True
        assert ctrl.consume_text_fallback("msg_fallback") is False
        assert "msg_fallback" not in ctrl._sessions

    @pytest.mark.asyncio
    async def test_generic_failure_marks_failed(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._client = None
        session = _make_session("msg_err")
        ctrl._sessions["msg_err"] = session

        await ctrl._do_create_card(session)

        assert session.state == SessionState.FAILED

    @pytest.mark.asyncio
    async def test_segment_state_set_before_await(self) -> None:
        """CREATING 期间的事件进入流式卡片路径 — segment_state 在 try 之前设置."""
        ctrl = _setup_ctrl()
        session = _make_session("msg_early")
        ctrl._sessions["msg_early"] = session

        original_ensure = ctrl._ensure_init

        async def check_state_then_ensure() -> None:
            assert session.segment_state is not None
            await original_ensure()

        ctrl._ensure_init = check_state_then_ensure  # type: ignore[assignment]
        await ctrl._do_create_card(session)

    @pytest.mark.asyncio
    async def test_post_create_flush_on_dirty(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_dirty")
        ctrl._sessions["msg_dirty"] = session

        original_ensure = ctrl._ensure_init

        async def inject_data_and_ensure() -> None:
            await original_ensure()
            session.segment_state.on_reasoning_delta("during-creating")

        ctrl._ensure_init = inject_data_and_ensure  # type: ignore[assignment]

        with patch.object(ctrl, "_schedule_flush") as m:
            await ctrl._do_create_card(session)
            m.assert_called()


# ── _do_flush 集成测试 ──


class TestDoFlush:
    @staticmethod
    def _tool_actions(ctrl: StreamCardController) -> list[dict]:
        return [
            action
            for call in ctrl._client.cardkit_batch_update.await_args_list
            for action in call.args[1]
            if action.get("params", {}).get("element_id") == TOOL_PANEL_ELEMENT_ID
            or any(
                element.get("element_id") == TOOL_PANEL_ELEMENT_ID
                for element in action.get("params", {}).get("elements", [])
            )
        ]

    @pytest.mark.asyncio
    async def test_tool_events_share_one_fixed_panel_and_follow_live_expansion(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_unified_tools")
        session.state = SessionState.STREAMING
        session.card_id = "card_unified_tools"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_tool_update(
                message_id=session.message_id, tool_name="read", status="started", detail="a.py"
            )
        await ctrl._do_flush(session)

        adds = [
            element
            for action in self._tool_actions(ctrl)
            for element in action.get("params", {}).get("elements", [])
            if element.get("element_id") == TOOL_PANEL_ELEMENT_ID
        ]
        assert len(adds) == 1
        assert adds[0]["expanded"] is True

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_tool_update(
                message_id=session.message_id, tool_name="read", status="completed", detail="ok"
            )
            assert ctrl.on_answer(message_id=session.message_id, text="answer")
        await ctrl._do_flush(session)
        partials = [
            action for action in self._tool_actions(ctrl)
            if action["action"] == "partial_update_element"
        ]
        assert partials[-1]["params"]["partial_element"]["expanded"] is False
        collapsed_count = len(partials)

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_answer(message_id=session.message_id, text=" answer 2")
        await ctrl._do_flush(session)
        partials = [
            action for action in self._tool_actions(ctrl)
            if action["action"] == "partial_update_element"
        ]
        assert len(partials) == collapsed_count
        assert any("answer answer 2" in call.args[2] for call in ctrl._client.cardkit_stream_element.await_args_list)

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_tool_update(
                message_id=session.message_id, tool_name="exec", status="started", detail="pytest"
            )
        await ctrl._do_flush(session)
        partials = [
            action for action in self._tool_actions(ctrl)
            if action["action"] == "partial_update_element"
        ]
        assert partials[-1]["params"]["partial_element"]["expanded"] is True

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_tool_update(
                message_id=session.message_id, tool_name="exec", status="completed", detail="passed"
            )
        await ctrl._do_flush(session)
        partials = [
            action for action in self._tool_actions(ctrl)
            if action["action"] == "partial_update_element"
        ]
        assert partials[-1]["params"]["partial_element"]["expanded"] is False
        collapsed_count = len(partials)

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_answer(message_id=session.message_id, text=" answer 3")
        await ctrl._do_flush(session)
        partials = [
            action for action in self._tool_actions(ctrl)
            if action["action"] == "partial_update_element"
        ]
        assert len(partials) == collapsed_count
        assert len(adds) == 1
        assert [seg.type for seg in session.segment_state.segments] == [
            "tool", "answer", "tool", "answer",
        ]

    @pytest.mark.asyncio
    async def test_hidden_detail_keeps_one_panel_and_reduces_estimate(self) -> None:
        ctrl = _setup_ctrl()
        _configure_progress(ctrl, show_tool_detail=False)
        session = _make_session("msg_hidden_detail")
        session.state = SessionState.STREAMING
        session.card_id = "card_hidden_detail"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_tool_update(
                message_id=session.message_id,
                tool_name="exec",
                status="started",
                detail="python3 /tmp/anysearch_cli.py batch_search --queries '[payload]'",
            )
        await ctrl._do_flush(session)

        adds = [
            element
            for action in self._tool_actions(ctrl)
            for element in action.get("params", {}).get("elements", [])
            if element.get("element_id") == TOOL_PANEL_ELEMENT_ID
        ]
        assert len(adds) == 1
        assert "Run command" in str(adds[0])
        assert "anysearch_cli.py" not in str(adds[0])
        assert session.tool_panel.element_estimate == 6

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_tool_update(
                message_id=session.message_id,
                tool_name="exec",
                status="completed",
                detail="ignored",
            )
        await ctrl._do_flush(session)
        assert "anysearch_cli.py" not in str(self._tool_actions(ctrl)[-1])

    @pytest.mark.asyncio
    async def test_compact_detail_is_used_by_controller_panel(self) -> None:
        ctrl = _setup_ctrl()
        _configure_progress(ctrl, tool_detail_mode="compact")
        session = _make_session("msg_compact_detail")
        session.state = SessionState.STREAMING
        session.card_id = "card_compact_detail"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_tool_update(
                message_id=session.message_id,
                tool_name="exec",
                status="started",
                detail="python3 /tmp/anysearch_cli.py batch_search --queries '[payload]'",
            )
        await ctrl._do_flush(session)

        add = next(
            element
            for action in self._tool_actions(ctrl)
            for element in action.get("params", {}).get("elements", [])
            if element.get("element_id") == TOOL_PANEL_ELEMENT_ID
        )
        assert "anysearch_cli.py batch_search" in str(add)
        assert "--queries" not in str(add)
        assert session.tool_panel.element_estimate == 8

    @pytest.mark.asyncio
    async def test_answer_deltas_collapse_tool_panel_only_once(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_answer_deltas")
        session.state = SessionState.STREAMING
        session.card_id = "card_answer_deltas"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_tool_update(
                message_id=session.message_id, tool_name="read", status="started", detail="a.py"
            )
        await ctrl._do_flush(session)

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_tool_update(
                message_id=session.message_id, tool_name="read", status="completed", detail="ok"
            )
        await ctrl._do_flush(session)

        for text in (" ", "\n"):
            with patch.object(ctrl, "_schedule_flush"):
                assert ctrl.on_answer(message_id=session.message_id, text=text)
            await ctrl._do_flush(session)

        whitespace_partials = [
            action for action in self._tool_actions(ctrl)
            if action["action"] == "partial_update_element"
        ]
        assert whitespace_partials[-1]["params"]["partial_element"]["expanded"] is True

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_answer(message_id=session.message_id, text="hello")
        await ctrl._do_flush(session)
        first_answer_partials = [
            action for action in self._tool_actions(ctrl)
            if action["action"] == "partial_update_element"
        ]
        assert len(first_answer_partials) == len(whitespace_partials) + 1
        assert first_answer_partials[-1]["params"]["partial_element"]["expanded"] is False

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_answer(message_id=session.message_id, text=" world")
        await ctrl._do_flush(session)

        later_partials = [
            action for action in self._tool_actions(ctrl)
            if action["action"] == "partial_update_element"
        ]
        assert len(later_partials) == len(first_answer_partials)
        assert any(
            "hello world" in call.args[2]
            for call in ctrl._client.cardkit_stream_element.await_args_list
        )

    @pytest.mark.asyncio
    async def test_show_tool_use_false_keeps_tracker_and_hides_panel(self) -> None:
        ctrl = _setup_ctrl()
        _configure_progress(ctrl, show_tool_use=False)
        session = _make_session("msg_hidden_tools")
        session.state = SessionState.STREAMING
        session.card_id = "card_hidden_tools"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_tool_update(
                message_id=session.message_id, tool_name="read", status="started", detail="a.py"
            )
            assert ctrl.on_tool_update(
                message_id=session.message_id, tool_name="read", status="completed", detail="ok"
            )
        await ctrl._do_flush(session)

        assert len(session.tool_use.build_display_steps()) == 1
        assert not self._tool_actions(ctrl)
        assert session.segment_state.segments[0].type == "tool"

    @pytest.mark.asyncio
    async def test_missing_fixed_tool_panel_is_recreated_without_failing_session(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_missing_tool_panel")
        session.state = SessionState.STREAMING
        session.card_id = "card_missing_tool_panel"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session
        ctrl._client.cardkit_batch_update = AsyncMock(
            side_effect=[
                FeishuAPIError(
                    f"cardkit_batch_update: code=300313, msg=ErrMsg: not find elementID : {TOOL_PANEL_ELEMENT_ID};",
                    300313,
                ),
                None,
            ]
        )

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_tool_update(
                message_id=session.message_id, tool_name="read", status="started", detail="a.py"
            )
        await ctrl._do_flush(session)
        assert session.tool_panel.created is False
        assert session.tool_panel.dirty is True
        assert session.state == SessionState.STREAMING

        await ctrl._do_flush(session)
        assert session.tool_panel.created is True
        assert session.tool_panel.dirty is False
        assert ctrl._client.cardkit_batch_update.await_count == 2

    @pytest.mark.asyncio
    async def test_three_step_pipeline(self) -> None:
        """step1 创建元素 → step2 刷文本 → step3 创建 tool 面板."""
        ctrl = _setup_ctrl()
        session = _make_session("msg_flush")
        session.state = SessionState.STREAMING
        session.card_id = "card_flush"
        session.segment_state.on_reasoning_delta("think")
        session.segment_state.on_answer_delta("hello world")
        session.tool_use.record_start("read", "f")
        session.segment_state.on_tool_event(1)
        ctrl._sessions["msg_flush"] = session

        await ctrl._do_flush(session)

        # step1: elements created
        assert session.segment_state.segments[0].created is True
        assert session.segment_state.segments[1].created is True
        # step2: dirty cleared for reasoning + answer
        assert session.segment_state.segments[0].dirty is False
        assert session.segment_state.segments[1].dirty is False
        # step2: stream_element called with answer text
        ctrl._client.cardkit_stream_element.assert_called()
        assert "hello world" in ctrl._client.cardkit_stream_element.call_args[0][2]
        # step3: tool created
        tool_seg = session.segment_state.segments[2]
        assert tool_seg.created is True

    @pytest.mark.asyncio
    async def test_no_split_keeps_original_single_card_flow(self) -> None:
        """低于阈值时仍是原来的单卡 flush：只 batch/stream 当前 card，不触发拆卡 API."""
        ctrl = _setup_ctrl()
        session = _make_session("msg_no_split")
        session.state = SessionState.STREAMING
        session.card_id = "card_no_split"
        session.element_count = 1
        session.segment_state.on_reasoning_delta("think")
        session.segment_state.on_answer_delta("hello")
        ctrl._sessions["msg_no_split"] = session

        await ctrl._do_flush(session)

        assert session.split_index == 0
        assert session.card_id == "card_no_split"
        assert [s.created for s in session.segment_state.segments] == [True, True]
        assert [s.dirty for s in session.segment_state.segments] == [False, False]
        ctrl._client.cardkit_create.assert_not_called()
        ctrl._client.reply_card_by_id.assert_not_called()
        ctrl._client.cardkit_close_streaming.assert_not_called()
        ctrl._client.cardkit_update.assert_not_called()
        ctrl._client.cardkit_batch_update.assert_called_once()
        assert ctrl._client.cardkit_stream_element.call_count == 2

    @pytest.mark.asyncio
    async def test_split_flushes_pending_actions_then_moves_to_next_card(self) -> None:
        """超阈值时先把 pending segment 写入旧卡，再封旧卡并把后续 segment 写入新卡."""
        ctrl = _setup_ctrl()
        calls = _capture_split_calls(ctrl)

        session = _make_session("msg_split")
        session.state = SessionState.STREAMING
        session.card_id = "card_old"
        session.card_msg_id = "msg_old"
        session.element_count = 174
        session.segment_state.on_reasoning_delta("old")
        session.segment_state.segments[0].created = True
        session.segment_state.segments[0].dirty = False
        session.segment_state.on_answer_delta("pending answer")
        session.tool_use.record_start("read", "file")
        session.segment_state.on_tool_event(1)
        ctrl._sessions["msg_split"] = session

        await ctrl._do_flush(session)

        assert calls == [
            ("batch", "card_old"),
            ("create", ""),
            ("reply", ""),
            ("close", "card_old"),
            ("seal", "card_old"),
            ("batch", "card_next"),
        ]
        assert session.card_id == "card_next"
        assert session.card_msg_id == "msg_next"
        assert session.split_index == 2
        assert session.split_disabled is False
        assert session.element_count > 1
        assert [s.created for s in session.segment_state.segments] == [True, True, True]

    @pytest.mark.asyncio
    async def test_second_split_seals_only_current_card_segments(self) -> None:
        """多次拆卡时，seal 不应重复包含更早卡片上的 segments."""
        ctrl = _setup_ctrl()
        ctrl._cfg._raw["streaming"]["width_mode"] = "compact"
        sealed_cards: list[dict] = []
        ctrl._client.cardkit_create = AsyncMock(return_value="card_page_3")
        ctrl._client.reply_card_by_id = AsyncMock(return_value="msg_page_3")
        ctrl._client.cardkit_update = AsyncMock(
            side_effect=lambda _card_id, card, **_kwargs: sealed_cards.append(card)
        )

        session = _make_session("msg_second_split")
        session.state = SessionState.STREAMING
        session.card_id = "card_page_2"
        session.card_msg_id = "msg_page_2"
        session.split_index = 2
        for index in range(5):
            seg = Segment("answer", f"answer_{index}")
            seg.text = f"page content {index}"
            seg.created = True
            seg.dirty = False
            session.segment_state.segments.append(seg)

        assert await ctrl._do_split_card(session, 5, [], set(), {}, []) is True

        contents = [element["content"] for element in sealed_cards[0]["body"]["elements"]]
        assert contents == ["page content 2", "page content 3", "page content 4"]
        assert sealed_cards[0]["config"]["width_mode"] == "compact"
        next_card = ctrl._client.cardkit_create.await_args.args[0]
        assert next_card["config"]["width_mode"] == "compact"
        assert session.split_index == 5

    @pytest.mark.asyncio
    async def test_tool_growth_updates_one_bounded_panel_on_the_same_card(self) -> None:
        """Tool chronology growth updates the fixed panel instead of rolling over."""
        ctrl = _setup_ctrl()
        calls = _capture_split_calls(
            ctrl,
            cards=["card_tool_next"],
            messages=["msg_tool_next"],
        )

        session = _make_session("msg_tool_roll")
        session.state = SessionState.STREAMING
        session.card_id = "card_tool_old"
        session.card_msg_id = "msg_tool_old"
        session.tool_use.record_start("read", "file0")
        session.segment_state.on_tool_event(1)
        tool_seg = session.segment_state.segments[0]
        tool_seg.created = True
        tool_seg.element_estimate = estimate_segment_elements(tool_seg, session.tool_use.build_display_steps())
        session.element_count = 1

        for idx in range(1, 4):
            session.tool_use.record_start("read", f"file{idx}")
        session.segment_state.on_tool_event(len(session.tool_use.build_display_steps()))
        ctrl._sessions["msg_tool_roll"] = session

        await ctrl._do_flush(session)

        assert calls == [("batch", "card_tool_old")]
        assert session.card_id == "card_tool_old"
        assert session.split_index == 0
        assert len(session.segment_state.segments) == 1
        assert session.segment_state.segments[0].created is True

    @pytest.mark.asyncio
    async def test_flush_renders_the_snapshot_policy_not_the_original_config(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_projection_flags")
        session.state = SessionState.STREAMING
        session.card_id = "card_projection_flags"
        session.element_count = 1
        for index in range(50):
            session.tool_use.record_start("exec", f"command-{index}")
            session.tool_use.record_end("exec", output=f"result-{index}")
        session.segment_state.on_tool_event(50)
        source_steps = session.tool_use.build_display_steps()
        expected = project_tool_panel(
            source_steps,
            show_tool_detail=True,
            tool_detail_mode="full",
            element_budget=ELEMENT_THRESHOLD - FOOTER_RESERVE - 1,
        )
        ctrl._sessions[session.message_id] = session

        await ctrl._do_flush(session)

        actions = ctrl._client.cardkit_batch_update.await_args.args[1]
        panel = next(
            action["params"]["elements"][0]
            for action in actions
            if action["action"] == "add_elements"
            and action["params"]["elements"][0].get("element_id") == TOOL_PANEL_ELEMENT_ID
        )
        actual_estimate = estimate_cardkit_elements({"body": {"elements": [panel]}})
        assert expected.mode.value == "title_only"
        assert actual_estimate == expected.estimated_elements
        assert all("margin" not in element for element in panel["elements"])

    @pytest.mark.asyncio
    async def test_split_preloads_meaningful_tool_snapshot_without_second_rollover(self) -> None:
        """An exhausted card creates its successor with a tool snapshot immediately."""
        ctrl = _setup_ctrl()
        calls = _capture_split_calls(
            ctrl,
            cards=["card_tool_pending_next"],
            messages=["msg_tool_pending_next"],
        )

        session = _make_session("msg_tool_pending_roll")
        session.state = SessionState.STREAMING
        session.card_id = "card_tool_pending_old"
        session.card_msg_id = "msg_tool_pending_old"
        session.element_count = 175
        session.segment_state.on_answer_delta("pending answer")
        session.tool_use.record_start("read", "file0")
        session.segment_state.on_tool_event(1)
        tool_seg = session.segment_state.segments[1]
        tool_seg.created = True
        tool_seg.element_estimate = estimate_segment_elements(tool_seg, session.tool_use.build_display_steps())

        session.tool_use.record_start("read", "file1")
        session.segment_state.on_tool_event(len(session.tool_use.build_display_steps()))
        ctrl._sessions["msg_tool_pending_roll"] = session

        await ctrl._do_flush(session)

        assert calls == [
            ("create", ""),
            ("reply", ""),
            ("close", "card_tool_pending_old"),
            ("seal", "card_tool_pending_old"),
            ("batch", "card_tool_pending_next"),
        ]
        assert session.card_id == "card_tool_pending_next"
        assert session.split_index == 0
        assert len(session.segment_state.segments) == 2
        assert session.segment_state.segments[1].tool_offset == 0
        assert session.segment_state.segments[1].tool_end_offset == 0
        assert session.tool_panel.created is True
        assert session.tool_panel.element_estimate > 0
        assert calls.count(("create", "")) == 1

    @pytest.mark.asyncio
    async def test_split_seal_and_preload_render_bounded_snapshot_policy(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_split_policy")
        session.state = SessionState.STREAMING
        session.card_id = "card_split_policy"
        session.card_msg_id = "msg_split_policy"
        session.element_count = 175
        session.segment_state.on_answer_delta("answer before split")
        for index in range(50):
            session.tool_use.record_start("exec", f"command-{index}")
            session.tool_use.record_end("exec", output=f"result-{index}")
        session.segment_state.on_tool_event(50)
        ctrl._sessions[session.message_id] = session

        await ctrl._do_flush(session)

        seal_card = ctrl._client.cardkit_update.await_args.args[1]
        assert estimate_cardkit_elements(seal_card) <= ELEMENT_THRESHOLD
        preload_actions: list[list[dict]] = []
        for call in ctrl._client.cardkit_batch_update.await_args_list:
            actions = call.args[1]
            if any(
                action["action"] == "add_elements"
                and action["params"]["elements"][0].get("element_id") == TOOL_PANEL_ELEMENT_ID
                for action in actions
            ):
                preload_actions.append(actions)
        assert len(preload_actions) == 1
        panel = next(
            element
            for action in preload_actions[0]
            if action["action"] == "add_elements"
            for element in action["params"]["elements"]
            if element.get("element_id") == TOOL_PANEL_ELEMENT_ID
        )
        assert estimate_cardkit_elements({"body": {"elements": [panel]}}) <= 174
        assert all("margin" not in element for element in panel["elements"])

    @pytest.mark.asyncio
    async def test_split_seal_bounds_rich_tool_history(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_split_seal_tools")
        session.state = SessionState.STREAMING
        session.card_id = "card_split_seal_tools"
        session.card_msg_id = "msg_split_seal_tools"
        for index in range(50):
            session.tool_use.record_start("exec", f"command-{index}")
            session.tool_use.record_end("exec", output=f"result-{index}")
        session.segment_state.on_tool_event(50)
        tool_segment = session.segment_state.segments[0]
        tool_segment.created = True

        assert await ctrl._do_split_card(session, 1, [], set(), {}, []) is True

        seal_card = ctrl._client.cardkit_update.await_args.args[1]
        seal_panel = next(
            element for element in seal_card["body"]["elements"]
            if element.get("element_id") == TOOL_PANEL_ELEMENT_ID
        )
        assert estimate_cardkit_elements(seal_card) <= ELEMENT_THRESHOLD
        assert "50 steps" in seal_panel["header"]["title"]["content"]
        assert all("margin" not in element for element in seal_panel["elements"])

    @pytest.mark.asyncio
    async def test_oversized_tool_chronology_uses_one_windowed_panel(self) -> None:
        """Many tools remain one physical card after bounded projection."""
        ctrl = _setup_ctrl()
        calls = _capture_split_calls(ctrl)

        session = _make_session("msg_tool_many")
        session.state = SessionState.STREAMING
        session.card_id = "card_tool_page_1"
        session.card_msg_id = "msg_tool_page_1"
        session.element_count = 1
        session.tool_use.record_start("check")
        session.segment_state.on_tool_event(1)
        for _ in range(127):
            session.tool_use.record_start("check")
        session.segment_state.on_tool_event(len(session.tool_use.build_display_steps()))
        ctrl._sessions["msg_tool_many"] = session

        await ctrl._do_flush(session)

        assert calls == [("batch", "card_tool_page_1")]
        assert session.card_id == "card_tool_page_1"
        assert session.card_msg_id == "msg_tool_page_1"
        assert session.split_index == 0
        assert len(session.segment_state.segments) == 1
        assert session.segment_state.segments[0].created is True
        assert session.tool_panel.element_estimate <= 174

    @pytest.mark.asyncio
    async def test_tool_split_create_failure_keeps_current_card_as_safe_fallback(self) -> None:
        """A genuinely exhausted physical card remains fail-open if split creation fails."""
        ctrl = _setup_ctrl()
        batch_card_ids = _capture_split_calls(ctrl, create_error=RuntimeError("create failed"))
        client = ctrl._client

        session = _make_session("msg_tool_roll_fallback")
        session.state = SessionState.STREAMING
        session.card_id = "card_tool_current"
        session.card_msg_id = "msg_tool_current"
        session.tool_use.record_start("read", "file0")
        session.segment_state.on_tool_event(1)
        tool_seg = session.segment_state.segments[0]
        tool_seg.created = True
        tool_seg.element_estimate = estimate_segment_elements(tool_seg, session.tool_use.build_display_steps())
        session.element_count = 174

        for idx in range(1, 4):
            session.tool_use.record_start("read", f"file{idx}")
        session.segment_state.on_tool_event(len(session.tool_use.build_display_steps()))
        ctrl._sessions["msg_tool_roll_fallback"] = session

        await ctrl._do_flush(session)

        assert session.card_id == "card_tool_current"
        assert session.split_index == 0
        assert session.split_disabled is True
        assert len(session.segment_state.segments) == 1
        assert session.segment_state.segments[0].created is True
        assert batch_card_ids == [("batch", "card_tool_current")]
        client.cardkit_close_streaming.assert_not_called()
        client.cardkit_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_split_create_failure_falls_back_to_current_card(self) -> None:
        """新卡创建失败是有意降级：不推进 split_index，继续把后续内容写回当前卡."""
        ctrl = _setup_ctrl()
        batch_card_ids = _capture_split_calls(ctrl, create_error=RuntimeError("create failed"))
        client = ctrl._client

        session = _make_session("msg_split_fallback")
        session.state = SessionState.STREAMING
        session.card_id = "card_current"
        session.card_msg_id = "msg_current"
        session.element_count = 174
        session.segment_state.on_reasoning_delta("old")
        session.segment_state.segments[0].created = True
        session.segment_state.segments[0].dirty = False
        session.segment_state.on_answer_delta("pending answer")
        session.tool_use.record_start("read", "file")
        session.segment_state.on_tool_event(1)
        ctrl._sessions["msg_split_fallback"] = session

        await ctrl._do_flush(session)

        assert session.card_id == "card_current"
        assert session.card_msg_id == "msg_current"
        assert session.split_index == 0
        assert session.split_disabled is True
        assert session.element_count > 174
        assert batch_card_ids == [("batch", "card_current"), ("batch", "card_current")]
        assert session.segment_state.segments[2].created is True
        client.cardkit_close_streaming.assert_not_called()
        client.cardkit_update.assert_not_called()

        client.cardkit_create.reset_mock()
        session.segment_state.on_answer_delta(" after fallback")

        await ctrl._do_flush(session)

        client.cardkit_create.assert_not_called()
        assert batch_card_ids == [
            ("batch", "card_current"),
            ("batch", "card_current"),
            ("batch", "card_current"),
        ]
        assert session.segment_state.segments[-1].created is True

    @pytest.mark.asyncio
    async def test_batch_update_recovers_stale_segment_on_missing_element(self) -> None:
        """300313 (not find elementID) 后回滚 stale segment，下一轮 flush 走 add 重建，不再死循环."""
        ctrl = _setup_ctrl()
        client = ctrl._client

        session = _make_session("msg_stale")
        session.state = SessionState.STREAMING
        session.card_id = "card_stale"
        session.card_msg_id = "msg_stale_card"
        # 一个已创建的 tool segment（本地 created=True，但模拟卡片上已不存在）
        session.tool_use.record_start("read", "file0")
        session.segment_state.on_tool_event(1)
        tool_seg = session.segment_state.segments[0]
        tool_seg.created = True
        tool_seg.element_estimate = estimate_segment_elements(tool_seg, session.tool_use.build_display_steps())
        session.element_count = tool_seg.element_estimate
        ctrl._sessions["msg_stale"] = session

        stale_el_id = tool_seg.el_id
        # 第一次 batch_update 抛 300313 — 引用了卡片上不存在的 el_id
        client.cardkit_batch_update = AsyncMock(
            side_effect=[
                FeishuAPIError(
                    f"cardkit_batch_update: code=300313, msg=ErrMsg: not find elementID : {stale_el_id};",
                    300313,
                ),
                None,
            ]
        )

        # 新增 tool step 触发 dirty → flush 走 partial_update 分支 → 300313
        session.tool_use.record_start("read", "file1")
        session.segment_state.on_tool_event(len(session.tool_use.build_display_steps()))

        await ctrl._do_flush(session)

        # 回滚：stale segment 被标记为未创建 + 脏，等下一轮重建
        assert tool_seg.created is False
        assert tool_seg.dirty is True
        # element_count 同步扣减（避免下一轮 add 重复累加导致阈值虚高、误触发拆分）
        assert session.element_count == 0

        # 第二次 flush：走 add_elements 重建，batch_update 成功
        await ctrl._do_flush(session)

        assert tool_seg.created is True
        # 重建后 element_count = 当前 tool steps（2 个）的新估算值，无重复计数
        expected = estimate_segment_elements(tool_seg, session.tool_use.build_display_steps())
        assert session.element_count == expected
        # 两次 batch_update 调用：第一次失败，第二次成功（非死循环重试 N 次）
        assert client.cardkit_batch_update.await_count == 2

    @pytest.mark.asyncio
    async def test_reasoning_finalized_snapshot(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_snap")
        session.state = SessionState.STREAMING
        session.card_id = "card_snap"
        session.segment_state.on_reasoning_delta("think")
        session.segment_state.on_answer_delta("reply")
        session.segment_state.segments[0].elapsed_ms = 1500.0
        session.segment_state.segments[0].reasoning_finalized = False
        ctrl._sessions["msg_snap"] = session

        await ctrl._do_flush(session)

        assert session.segment_state.segments[0].reasoning_finalized is True

    @pytest.mark.asyncio
    async def test_reasoning_title_update_with_elapsed(self) -> None:
        ctrl = _setup_ctrl()
        batch_calls: list[list[dict]] = []

        async def capture_batch(card_id: str, actions: list[dict], **kw: object) -> None:
            batch_calls.append(actions)

        ctrl._client.cardkit_batch_update = capture_batch

        session = _make_session("msg_title")
        session.state = SessionState.STREAMING
        session.card_id = "card_title"
        session.segment_state.on_reasoning_delta("think")
        session.segment_state.on_answer_delta("reply")
        session.segment_state.segments[0].elapsed_ms = 2500.0
        session.segment_state.segments[0].created = True
        session.segment_state.segments[0].reasoning_finalized = False
        ctrl._sessions["msg_title"] = session

        await ctrl._do_flush(session)

        partials = [a for a in batch_calls[0] if a["action"] == "partial_update_element"]
        assert len(partials) == 1
        assert "2.5s" in partials[0]["params"]["partial_element"]["header"]["title"]["content"]

    @pytest.mark.asyncio
    async def test_tool_dirty_snapshot(self) -> None:
        """await 期间 tool_end_offset 变化 → dirty 保持."""
        ctrl = _setup_ctrl()
        original_batch = ctrl._client.cardkit_batch_update
        tool_seg_ref: Segment | None = None
        batch_counter = 0

        async def batch_with_race(card_id: str, actions: list[dict], **kw: object) -> None:
            nonlocal batch_counter
            await original_batch(card_id, actions, **kw)
            batch_counter += 1
            if batch_counter == 1 and tool_seg_ref is not None and tool_seg_ref.tool_end_offset == 0:
                tool_seg_ref.tool_end_offset = 5

        ctrl._client.cardkit_batch_update = batch_with_race

        session = _make_session("msg_tool_snap")
        session.state = SessionState.STREAMING
        session.card_id = "card_snap"
        session.segment_state.on_answer_delta("text")
        session.tool_use.record_start("read", "f")
        session.segment_state.on_tool_event(1)
        tool_seg_ref = session.segment_state.segments[1]
        ctrl._sessions["msg_tool_snap"] = session

        await ctrl._do_flush(session)

        assert tool_seg_ref.tool_end_offset == 5
        assert tool_seg_ref.dirty is True

    @pytest.mark.asyncio
    async def test_open_tool_dirty_cleared_when_steps_unchanged(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_open_tool_clean")
        session.state = SessionState.STREAMING
        session.card_id = "card_open_tool_clean"
        session.tool_use.record_start("read", "f")
        session.segment_state.on_tool_event(1)
        tool_seg = session.segment_state.segments[0]
        tool_seg.created = True
        ctrl._sessions["msg_open_tool_clean"] = session

        await ctrl._do_flush(session)

        assert tool_seg.tool_end_offset == 0
        assert tool_seg.dirty is False

    @pytest.mark.asyncio
    async def test_open_tool_dirty_snapshot_when_steps_change(self) -> None:
        """await 期间 open tool 新增 step → dirty 保持."""
        ctrl = _setup_ctrl()
        original_batch = ctrl._client.cardkit_batch_update
        session = _make_session("msg_open_tool_snap")
        session.state = SessionState.STREAMING
        session.card_id = "card_open_tool_snap"
        session.tool_use.record_start("read", "f")
        session.segment_state.on_tool_event(1)
        tool_seg = session.segment_state.segments[0]
        tool_seg.created = True

        async def batch_with_race(card_id: str, actions: list[dict], **kw: object) -> None:
            await original_batch(card_id, actions, **kw)
            session.tool_use.record_start("grep", "q")
            session.segment_state.on_tool_event(2)

        ctrl._client.cardkit_batch_update = batch_with_race
        ctrl._sessions["msg_open_tool_snap"] = session

        await ctrl._do_flush(session)

        assert tool_seg.tool_end_offset == 0
        assert tool_seg.dirty is True

    @pytest.mark.asyncio
    async def test_open_tool_dirty_snapshot_when_step_content_changes(self) -> None:
        """await 期间 open tool 完成但 step 数不变 → dirty 保持."""
        ctrl = _setup_ctrl()
        original_batch = ctrl._client.cardkit_batch_update
        session = _make_session("msg_open_tool_content_snap")
        session.state = SessionState.STREAMING
        session.card_id = "card_open_tool_content_snap"
        session.tool_use.record_start("read", "f")
        session.segment_state.on_tool_event(1)
        tool_seg = session.segment_state.segments[0]
        tool_seg.created = True

        async def batch_with_race(card_id: str, actions: list[dict], **kw: object) -> None:
            await original_batch(card_id, actions, **kw)
            session.tool_use.record_end("read", output="done")
            session.segment_state.on_tool_event(1)

        ctrl._client.cardkit_batch_update = batch_with_race
        ctrl._sessions["msg_open_tool_content_snap"] = session

        await ctrl._do_flush(session)

        assert tool_seg.tool_end_offset == 0
        assert tool_seg.dirty is True

    @pytest.mark.asyncio
    async def test_step2_exception_does_not_block_step3(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._client.cardkit_stream_element = AsyncMock(side_effect=RuntimeError("stream fail"))
        session = _make_session("msg_exc")
        session.state = SessionState.STREAMING
        session.card_id = "card_exc"
        session.segment_state.on_answer_delta("text")
        session.tool_use.record_start("read", "f")
        session.segment_state.on_tool_event(1)
        ctrl._sessions["msg_exc"] = session

        await ctrl._do_flush(session)

        assert ctrl._client.cardkit_batch_update.call_count >= 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [230020, 300309])
    async def test_api_errors_swallowed(self, code: int) -> None:
        """rate limited / streaming closed 不抛异常."""
        ctrl = _setup_ctrl()
        ctrl._client.cardkit_batch_update = AsyncMock(side_effect=FeishuAPIError("e", code=code))
        session = _make_session("msg_err")
        session.state = SessionState.STREAMING
        session.card_id = "card_e"
        session.segment_state.on_reasoning_delta("think")
        ctrl._sessions["msg_err"] = session

        await ctrl._do_flush(session)

    @pytest.mark.asyncio
    async def test_skip_conditions(self) -> None:
        """终态 / 无 card_id / 无 dirty 全部跳过 API 调用."""
        ctrl = _setup_ctrl()

        # 终态
        s1 = _make_session("m1")
        s1.state = SessionState.COMPLETED
        ctrl._sessions["m1"] = s1
        await ctrl._do_flush(s1)

        # 无 card_id
        s2 = _make_session("m2")
        s2.state = SessionState.STREAMING
        s2.card_id = None
        ctrl._sessions["m2"] = s2
        await ctrl._do_flush(s2)

        # 无 dirty
        s3 = _make_session("m3")
        s3.state = SessionState.STREAMING
        s3.card_id = "c"
        s3.segment_state.on_reasoning_delta("t")
        s3.segment_state.segments[0].created = True
        s3.segment_state.segments[0].dirty = False
        ctrl._sessions["m3"] = s3
        await ctrl._do_flush(s3)

        ctrl._client.cardkit_batch_update.assert_not_called()
        ctrl._client.cardkit_stream_element.assert_not_called()


# ── merged reasoning 集成测试 ──


class TestMergedReasoning:
    @pytest.mark.parametrize(
        ("api_mode", "expected"),
        [
            ("", False),
            ("chat_completions", False),
            ("codex_responses", True),
            ("codex_app_server", True),
        ],
    )
    def test_native_reasoning_policy_uses_api_mode(self, api_mode: str, expected: bool) -> None:
        assert StreamCardController._is_activity_reasoning_api_mode(api_mode) is expected

    @pytest.mark.parametrize(
        ("api_mode", "source", "expected"),
        [
            ("chat_completions", "native_reasoning", False),
            ("codex_responses", "native_reasoning", True),
            ("codex_app_server", "interim_commentary", False),
            ("chat_completions", "interim_commentary", False),
            ("codex_responses", "", False),
            ("", "interim_commentary", False),
        ],
    )
    def test_reasoning_presentation_requires_known_source_and_runtime(
        self,
        api_mode: str,
        source: str,
        expected: bool,
    ) -> None:
        assert StreamCardController._uses_activity_reasoning_presentation(
            api_mode=api_mode,
            source=source,
        ) is expected

    def test_interim_commentary_uses_preview_without_touching_merged_reasoning(self) -> None:
        ctrl = _setup_ctrl()
        _configure_merged(ctrl, show_reasoning=False)
        session = _make_session("msg_codex_commentary")
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_thinking(
                message_id=session.message_id,
                text="第 1 阶段\uFF1A读取项目配置",
                api_mode="codex_responses",
                source="interim_commentary",
            ) is True
        assert session.merged_reasoning.text == ""
        assert [seg.type for seg in session.segment_state.segments] == []
        assert session.interim_preview.text == "第 1 阶段\uFF1A读取项目配置"

    def test_native_reasoning_without_runtime_metadata_appends_safely(self) -> None:
        ctrl = _setup_ctrl()
        _configure_merged(ctrl)
        session = _make_session("msg_native_fallback")
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            for text in ("A", "B", "C"):
                assert ctrl.on_reasoning(
                    message_id=session.message_id,
                    text=text,
                    api_mode="",
                ) is True

        assert session.merged_reasoning.text == "ABC"

    @pytest.mark.asyncio
    async def test_native_reasoning_and_commentary_keep_separate_lanes_in_final_card(self) -> None:
        ctrl = _setup_ctrl()
        _configure_merged(ctrl, show_tool_use=False)
        session = CardSession("msg_mixed_channels", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_mixed_channels"
        session.card_msg_id = "card_msg_mixed_channels"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_reasoning(
                message_id=session.message_id,
                text="Planning staged skill loading and reading",
                api_mode="codex_responses",
            ) is True
            assert ctrl.on_thinking(
                message_id=session.message_id,
                text=_COMMENTARY_STAGE_1,
                api_mode="codex_responses",
                source="interim_commentary",
            ) is True
            assert ctrl.on_thinking(
                message_id=session.message_id,
                text=_COMMENTARY_CONFIRMED_1,
                api_mode="codex_responses",
                source="interim_commentary",
            ) is True
            assert ctrl.on_tool_update(
                message_id=session.message_id,
                tool_name="read",
                status="started",
            ) is True
            assert ctrl.on_tool_update(
                message_id=session.message_id,
                tool_name="read",
                status="completed",
            ) is True
            assert ctrl.on_reasoning(
                message_id=session.message_id,
                text="Planning explicit AGENTS file reading",
                api_mode="codex_responses",
            ) is True
            assert ctrl.on_thinking(
                message_id=session.message_id,
                text=_COMMENTARY_STAGE_2,
                api_mode="codex_responses",
                source="interim_commentary",
            ) is True
            assert ctrl.on_tool_update(
                message_id=session.message_id,
                tool_name="read",
                status="started",
            ) is True
            assert ctrl.on_tool_update(
                message_id=session.message_id,
                tool_name="read",
                status="completed",
            ) is True
            assert ctrl.on_reasoning(
                message_id=session.message_id,
                text="Comparing plugin and Hermes version requirements",
                api_mode="codex_responses",
            ) is True
            assert ctrl.on_answer(
                message_id=session.message_id,
                text=_FINAL_ANSWER,
            ) is True

        assert session.merged_reasoning.text == "Comparing plugin and Hermes version requirements"
        answer_segments = [
            seg.text for seg in session.segment_state.segments if seg.type == "answer"
        ]
        assert answer_segments == [_FINAL_ANSWER]
        assert session.interim_preview.final_started is True
        assert session.interim_preview.text == ""
        assert len(session.tool_use.build_display_steps()) == 2
        assert session.merged_reasoning.active_since is None

        await ctrl._do_flush(session)
        added_elements = [
            element
            for call in ctrl._client.cardkit_batch_update.await_args_list
            for action in call.args[1]
            for element in action.get("params", {}).get("elements", [])
        ]
        assert [
            element["element_id"]
            for element in added_elements
            if element.get("element_id") == REASONING_ELEMENT_ID
        ] == [REASONING_ELEMENT_ID]
        assert not any(str(element.get("element_id", "")).startswith("tools_") for element in added_elements)
        reasoning_streams = [
            call
            for call in ctrl._client.cardkit_stream_element.await_args_list
            if call.args[1] == REASONING_TEXT_ELEMENT_ID
        ]
        assert [call.args[2] for call in reasoning_streams] == [
            "Comparing plugin and Hermes version requirements"
        ]

        session.footer = {
            "duration": 26.5,
            "model": "gpt-5",
            "context_used": 50_000,
            "context_max": 200_000,
            "gpt_quota": "5h 80%",
        }
        ctrl._cfg._raw["streaming"]["footer"] = {
            "fields": [["status", "elapsed", "context", "gpt_quota", "model"]]
        }
        assert await ctrl._do_complete_card(session) is True
        complete_card = ctrl._client.cardkit_update.await_args.args[1]
        panels = [
            element
            for element in complete_card["body"]["elements"]
            if element.get("tag") == "collapsible_panel"
            and "💭" in element.get("header", {}).get("title", {}).get("content", "")
        ]
        assert len(panels) == 1
        assert panels[0]["elements"][0]["content"] == (
            "Comparing plugin and Hermes version requirements"
        )
        body_text = "\n".join(
            element.get("content", "")
            for element in complete_card["body"]["elements"]
            if element.get("tag") == "markdown"
        )
        assert _COMMENTARY_STAGE_1 not in body_text
        assert _COMMENTARY_CONFIRMED_1 not in body_text
        assert _COMMENTARY_STAGE_2 not in body_text
        assert "第 3 阶段\uFF1A比较与总结" in body_text
        assert "最终结论……" in body_text
        details = _run_details_panel(complete_card)
        assert details["expanded"] is False
        details_text = _run_details_text(details)
        assert "Context used 25%" in details_text

    @pytest.mark.asyncio
    async def test_codex_activity_uses_one_lane_across_hidden_tools_and_final_card(self) -> None:
        ctrl = _setup_ctrl()
        _configure_merged(ctrl, show_tool_use=False)
        session = CardSession("msg_merged", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_merged"
        session.card_msg_id = "card_msg_merged"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_reasoning(
                message_id=session.message_id,
                text="Planning",
                api_mode="codex_responses",
            ) is True
            assert ctrl.on_tool_update(
                message_id=session.message_id,
                tool_name="read",
                status="started",
            ) is True
            assert ctrl.on_reasoning(
                message_id=session.message_id,
                text="Checking",
                api_mode="codex_responses",
            ) is True
            assert ctrl.on_tool_update(
                message_id=session.message_id,
                tool_name="search",
                status="started",
            ) is True
            assert ctrl.on_reasoning(
                message_id=session.message_id,
                text="Confirming",
                api_mode="codex_responses",
            ) is True
            assert ctrl.on_answer(message_id=session.message_id, text="answer") is True

        await ctrl._do_flush(session)

        assert [seg.type for seg in session.segment_state.segments] == [
            "reasoning",
            "tool",
            "reasoning",
            "tool",
            "reasoning",
            "answer",
        ]
        assert [
            seg.text for seg in session.segment_state.segments if seg.type == "reasoning"
        ] == ["Planning", "Checking", "Confirming"]
        assert session.merged_reasoning.text == "Confirming"
        assert len(session.tool_use.build_display_steps()) == 2

        added_elements = [
            element
            for call in ctrl._client.cardkit_batch_update.await_args_list
            for action in call.args[1]
            for element in action.get("params", {}).get("elements", [])
        ]
        fixed_panels = [
            element
            for element in added_elements
            if element.get("element_id") == REASONING_ELEMENT_ID
        ]
        assert len(fixed_panels) == 1
        assert fixed_panels[0]["elements"][0]["element_id"] == REASONING_TEXT_ELEMENT_ID
        assert not any(
            (
                str(element.get("element_id", "")).startswith("reasoning_")
                and element.get("element_id") != REASONING_ELEMENT_ID
            )
            or str(element.get("element_id", "")).startswith("tools_")
            for element in added_elements
        )

        reasoning_streams = [
            call
            for call in ctrl._client.cardkit_stream_element.await_args_list
            if call.args[1] == REASONING_TEXT_ELEMENT_ID
        ]
        assert len(reasoning_streams) == 1
        assert reasoning_streams[0].args[2] == "Confirming"

        session.footer = {
            "duration": 26.5,
            "model": "gpt-5",
            "context_used": 50_000,
            "context_max": 200_000,
            "gpt_quota": "5h 80%",
        }
        ctrl._cfg._raw["streaming"]["footer"] = {
            "fields": [["status", "elapsed", "context", "gpt_quota", "model"]]
        }

        assert await ctrl._do_complete_card(session) is True
        complete_card = ctrl._client.cardkit_update.await_args.args[1]
        final_reasoning_panels = [
            element
            for element in complete_card["body"]["elements"]
            if element.get("tag") == "collapsible_panel"
            and "💭" in element.get("header", {}).get("title", {}).get("content", "")
        ]
        assert len(final_reasoning_panels) == 1
        assert final_reasoning_panels[0]["elements"][0]["content"] == "Confirming"
        details = _run_details_panel(complete_card)
        assert details["expanded"] is False
        details_text = _run_details_text(details)
        assert "Context used 25%" in details_text

    @pytest.mark.asyncio
    async def test_chat_completions_reasoning_deltas_append_in_merged_presentation(self) -> None:
        ctrl = _setup_ctrl()
        _configure_merged(ctrl)
        session = CardSession("msg_chat_deltas", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_chat_deltas"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_reasoning(
                message_id=session.message_id,
                text="A",
                api_mode="chat_completions",
            ) is True
            assert ctrl.on_reasoning(
                message_id=session.message_id,
                text="B",
                api_mode="chat_completions",
            ) is True
            assert ctrl.on_reasoning(
                message_id=session.message_id,
                text="C",
                api_mode="chat_completions",
            ) is True

        await ctrl._do_flush(session)

        assert session.segment_state.segments[0].text == "ABC"
        assert session.merged_reasoning.text == "ABC"
        reasoning_streams = [
            call
            for call in ctrl._client.cardkit_stream_element.await_args_list
            if call.args[1] == REASONING_TEXT_ELEMENT_ID
        ]
        assert [call.args[2] for call in reasoning_streams] == ["ABC"]

        assert await ctrl._do_complete_card(session) is True
        complete_card = ctrl._client.cardkit_update.await_args.args[1]
        final_reasoning_panels = [
            element
            for element in complete_card["body"]["elements"]
            if element.get("tag") == "collapsible_panel"
            and "💭" in element.get("header", {}).get("title", {}).get("content", "")
        ]
        assert len(final_reasoning_panels) == 1
        assert final_reasoning_panels[0]["elements"][0]["content"] == "ABC"

    @pytest.mark.asyncio
    async def test_chat_completions_deltas_survive_tool_interruption(self) -> None:
        ctrl = _setup_ctrl()
        _configure_merged(ctrl)
        session = CardSession("msg_chat_tools", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_chat_tools"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            for text in ("R1", "R2"):
                assert ctrl.on_reasoning(
                    message_id=session.message_id,
                    text=text,
                    api_mode="chat_completions",
                ) is True
            assert session.merged_reasoning.active_since is not None
            assert ctrl.on_tool_update(
                message_id=session.message_id,
                tool_name="read",
                status="started",
            ) is True
            assert session.merged_reasoning.active_since is None
            for text in ("R3", "R4"):
                assert ctrl.on_reasoning(
                    message_id=session.message_id,
                    text=text,
                    api_mode="chat_completions",
                ) is True
            assert session.merged_reasoning.active_since is not None
            assert ctrl.on_answer(message_id=session.message_id, text="answer") is True

        assert session.merged_reasoning.active_since is None
        assert session.merged_reasoning.text == "R1R2R3R4"
        assert [seg.type for seg in session.segment_state.segments] == [
            "reasoning",
            "tool",
            "reasoning",
            "answer",
        ]
        assert [
            seg.text for seg in session.segment_state.segments if seg.type == "reasoning"
        ] == ["R1R2", "R3R4"]
        await ctrl._do_flush(session)
        fixed_panels = [
            element
            for call in ctrl._client.cardkit_batch_update.await_args_list
            for action in call.args[1]
            for element in action.get("params", {}).get("elements", [])
            if element.get("element_id") == REASONING_ELEMENT_ID
        ]
        assert len(fixed_panels) == 1
        reasoning_streams = [
            call
            for call in ctrl._client.cardkit_stream_element.await_args_list
            if call.args[1] == REASONING_TEXT_ELEMENT_ID
        ]
        assert [call.args[2] for call in reasoning_streams] == ["R1R2R3R4"]

    @pytest.mark.asyncio
    async def test_segmented_native_reasoning_stays_on_numbered_panels(self) -> None:
        ctrl = _setup_ctrl()
        session = CardSession("msg_segmented_native", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_segmented_native"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_reasoning(
                message_id=session.message_id,
                text="Planning",
                api_mode="chat_completions",
            ) is True
            assert ctrl.on_tool_update(
                message_id=session.message_id,
                tool_name="read",
                status="started",
            ) is True
            assert ctrl.on_reasoning(
                message_id=session.message_id,
                text="Checking",
                api_mode="chat_completions",
            ) is True

        await ctrl._do_flush(session)

        assert session.merged_reasoning.text == ""
        added_element_ids = [
            element.get("element_id")
            for call in ctrl._client.cardkit_batch_update.await_args_list
            for action in call.args[1]
            for element in action.get("params", {}).get("elements", [])
        ]
        assert "reasoning_0_panel" in added_element_ids
        assert "reasoning_2_panel" in added_element_ids
        assert REASONING_ELEMENT_ID not in added_element_ids

    @pytest.mark.asyncio
    async def test_show_reasoning_false_keeps_answer_and_creates_no_reasoning_ui(self) -> None:
        ctrl = _setup_ctrl()
        _configure_merged(ctrl, show_reasoning=False)
        session = CardSession("msg_hidden_reasoning", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_hidden_reasoning"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_reasoning(message_id=session.message_id, text="secret") is False
            assert ctrl.on_answer(message_id=session.message_id, text="visible") is True

        await ctrl._do_flush(session)

        assert session.merged_reasoning.text == ""
        assert [seg.type for seg in session.segment_state.segments] == ["answer"]
        assert all(
            call.args[1] != REASONING_TEXT_ELEMENT_ID
            for call in ctrl._client.cardkit_stream_element.await_args_list
        )

    @pytest.mark.asyncio
    async def test_reasoning_create_failure_is_recoverable_and_answer_continues(self) -> None:
        ctrl = _setup_ctrl()
        _configure_merged(ctrl)
        session = CardSession("msg_merged_failure", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_merged_failure"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            ctrl.on_reasoning(message_id=session.message_id, text="reasoning")
            ctrl.on_answer(message_id=session.message_id, text="answer")

        failed_once = False
        batch_calls: list[list[dict]] = []

        async def fail_first_reasoning_add(
            _card_id: str,
            actions: list[dict],
            **_kwargs: object,
        ) -> None:
            nonlocal failed_once
            batch_calls.append(actions)
            adds_fixed_reasoning = any(
                element.get("element_id") == REASONING_ELEMENT_ID
                for action in actions
                for element in action.get("params", {}).get("elements", [])
            )
            if adds_fixed_reasoning and not failed_once:
                failed_once = True
                raise FeishuAPIError("rate limited", code=230020)

        ctrl._client.cardkit_batch_update = fail_first_reasoning_add

        await ctrl._do_flush(session)

        answer_seg = session.segment_state.segments[-1]
        assert answer_seg.type == "answer"
        assert answer_seg.created is True
        assert answer_seg.dirty is False
        assert session.merged_reasoning.created is False
        assert session.merged_reasoning.dirty is True

        await ctrl._do_flush(session)

        assert session.merged_reasoning.created is True
        assert session.merged_reasoning.dirty is False
        assert len(batch_calls) == 3

    @pytest.mark.asyncio
    async def test_reasoning_delta_during_stream_remains_dirty_until_next_flush(self) -> None:
        ctrl = _setup_ctrl()
        _configure_merged(ctrl)
        session = CardSession("msg_merged_race", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_merged_race"
        session.element_count = 1
        ctrl._append_reasoning(session, "A")

        streamed: list[str] = []

        async def append_during_first_stream(
            _card_id: str,
            _element_id: str,
            content: str,
            **_kwargs: object,
        ) -> None:
            streamed.append(content)
            if len(streamed) == 1:
                ctrl._append_reasoning(session, "B")

        ctrl._client.cardkit_stream_element = append_during_first_stream

        await ctrl._do_flush(session)

        assert session.merged_reasoning.text == "AB"
        assert session.merged_reasoning.dirty is True

        await ctrl._do_flush(session)

        assert streamed == ["A", "AB"]
        assert session.merged_reasoning.dirty is False
        assert ctrl._client.cardkit_batch_update.await_count == 1

    @pytest.mark.asyncio
    async def test_missing_fixed_text_element_recreates_lane_on_next_flush(self) -> None:
        ctrl = _setup_ctrl()
        _configure_merged(ctrl)
        session = CardSession("msg_merged_missing", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_merged_missing"
        session.element_count = 5
        ctrl._append_reasoning(session, "reasoning")
        session.merged_reasoning.created = True
        ctrl._client.cardkit_stream_element = AsyncMock(
            side_effect=[
                FeishuAPIError(
                    "cardkit_stream_element: code=300313, "
                    f"msg=ErrMsg: not find elementID : {REASONING_TEXT_ELEMENT_ID};",
                    300313,
                ),
                None,
            ]
        )

        await ctrl._do_flush(session)

        assert session.merged_reasoning.created is False
        assert session.merged_reasoning.dirty is True
        assert session.element_count == 1

        await ctrl._do_flush(session)

        assert session.merged_reasoning.created is True
        assert session.merged_reasoning.dirty is False
        assert session.element_count == 5
        assert ctrl._client.cardkit_batch_update.await_count == 1

    @pytest.mark.asyncio
    async def test_split_rebuilds_fixed_lane_without_losing_accumulated_text(self) -> None:
        ctrl = _setup_ctrl()
        _configure_merged(ctrl)
        session = CardSession("msg_merged_split", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_merged_old"
        session.card_msg_id = "card_msg_merged_old"
        session.element_count = 5
        ctrl._append_reasoning(session, "R1")
        ctrl._pause_merged_reasoning(session)
        session.segment_state.on_answer_delta("old answer")
        for seg in session.segment_state.segments:
            seg.created = True
            seg.dirty = False
        session.merged_reasoning.created = True
        session.merged_reasoning.dirty = False

        assert await ctrl._do_split_card(session, 2, [], set(), {}, []) is True

        sealed_card = ctrl._client.cardkit_update.await_args.args[1]
        sealed_reasoning = [
            element
            for element in sealed_card["body"]["elements"]
            if element.get("tag") == "collapsible_panel"
            and "💭" in element.get("header", {}).get("title", {}).get("content", "")
        ]
        assert len(sealed_reasoning) == 1
        assert sealed_reasoning[0]["elements"][0]["content"] == "R1"
        assert session.merged_reasoning.text == "R1"
        assert session.merged_reasoning.created is True
        fixed_adds = [
            element
            for call in ctrl._client.cardkit_batch_update.await_args_list
            for action in call.args[1]
            for element in action.get("params", {}).get("elements", [])
            if element.get("element_id") == REASONING_ELEMENT_ID
        ]
        assert len(fixed_adds) == 1
        assert ctrl._client.cardkit_stream_element.await_args.args[1] == REASONING_TEXT_ELEMENT_ID
        assert ctrl._client.cardkit_stream_element.await_args.args[2] == "R1"


# ── _do_complete_card 集成测试 ──


class TestDoCompleteCard:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("step_count", [50, 100])
    async def test_terminal_projects_full_chronology_into_a_bounded_tool_panel(
        self, step_count: int,
    ) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._raw["streaming"]["reasoning_mode"] = "merged"
        session = _make_session("msg_terminal_projection")
        session.state = SessionState.STREAMING
        session.card_id = "card_terminal_projection"
        session.segment_state.on_reasoning_delta("reasoning")
        session.tool_use.record_start("exec", "before-final")
        session.tool_use.record_end("exec", output="preserved-result")
        for index in range(step_count - 1):
            session.tool_use.record_start("exec", f"tool-{index}")
            session.tool_use.record_end("exec", output=f"result-{index}")
        session.segment_state.on_tool_event(step_count)
        session.interim_preview.replace("interim commentary")
        session.interim_preview.start_final()
        session.segment_state.on_answer_delta("true final answer")
        session.footer = {"duration": 1.0}
        source_steps = session.tool_use.build_display_steps()
        ctrl._sessions[session.message_id] = session

        assert len(source_steps) == step_count
        assert all(step["result_block"] is not None for step in source_steps)
        assert await ctrl._do_complete_card(session) is True

        card = ctrl._client.cardkit_update.await_args.args[1]
        tool_panel = next(
            element for element in card["body"]["elements"]
            if element.get("element_id") == TOOL_PANEL_ELEMENT_ID
        )
        body_text = "\n".join(
            element.get("content", "")
            for element in card["body"]["elements"]
            if element.get("tag") == "markdown"
        )
        assert f"{step_count} steps" in tool_panel["header"]["title"]["content"]
        assert estimate_cardkit_elements(card) <= ELEMENT_THRESHOLD
        assert "true final answer" in body_text
        assert "interim commentary" not in body_text
        assert "Done" not in body_text
        assert _run_details_panel(card)

    @pytest.mark.asyncio
    async def test_closes_streaming_then_updates(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._raw["streaming"]["width_mode"] = "compact"
        call_order: list[str] = []
        client = ctrl._client
        client.cardkit_close_streaming = AsyncMock(side_effect=lambda *a, **k: call_order.append("close"))
        client.cardkit_update = AsyncMock(side_effect=lambda *a, **k: call_order.append("update"))

        session = _make_session("msg_comp")
        session.state = SessionState.STREAMING
        session.card_id = "card_comp"
        session.card_msg_id = "msg_comp_reply"
        ctrl._sessions["msg_comp"] = session

        assert await ctrl._do_complete_card(session) is True
        assert session.state == SessionState.COMPLETED
        assert call_order == ["close", "update"]
        card = client.cardkit_update.await_args.args[1]
        assert card["config"]["width_mode"] == "compact"

    @pytest.mark.asyncio
    async def test_streaming_closed_flag_prevents_double_close(self) -> None:
        ctrl = _setup_ctrl()
        client = ctrl._client
        client.cardkit_close_streaming = AsyncMock()
        call_count = 0
        original_update = client.cardkit_update

        async def flaky_update(*args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise FeishuAPIError("conflict", code=300317)
            return await original_update(*args, **kwargs)

        client.cardkit_update = flaky_update

        session = _make_session("msg_retry")
        session.state = SessionState.STREAMING
        session.card_id = "card_retry"
        session.card_msg_id = "msg_retry_reply"
        ctrl._sessions["msg_retry"] = session

        assert await ctrl._do_complete_card(session) is True
        assert client.cardkit_close_streaming.call_count == 1
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_three_retries_exhausted(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._client.cardkit_close_streaming = AsyncMock(side_effect=FeishuAPIError("fail", code=99999))

        session = _make_session("msg_3fail")
        session.state = SessionState.STREAMING
        session.card_id = "card_3fail"
        ctrl._sessions["msg_3fail"] = session

        with patch("asyncio.sleep", new_callable=AsyncMock):
            assert await ctrl._do_complete_card(session) is False
        assert session.state == SessionState.FAILED

    @pytest.mark.asyncio
    async def test_finalize_and_cleanup(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_fc")
        session.state = SessionState.STREAMING
        session.card_id = "card_fc"
        session.segment_state.on_reasoning_delta("think")
        time.sleep(0.001)
        ctrl._sessions["msg_fc"] = session

        await ctrl._do_complete_card(session)

        assert session.segment_state.segments[0].elapsed_ms > 0
        assert "msg_fc" not in ctrl._sessions

    @pytest.mark.asyncio
    async def test_no_card_id_skips_close(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_nocard")
        session.state = SessionState.STREAMING
        session.card_id = None
        ctrl._sessions["msg_nocard"] = session

        assert await ctrl._do_complete_card(session) is True
        assert session.state == SessionState.COMPLETED
        ctrl._client.cardkit_close_streaming.assert_not_called()

    @pytest.mark.asyncio
    async def test_image_resolve_per_segment(self) -> None:
        """单个 segment resolve 失败不影响后续."""
        from unittest.mock import MagicMock

        ctrl = _setup_ctrl()
        session = _make_session("msg_img")
        session.state = SessionState.STREAMING
        session.card_id = "card_img"
        session.segment_state.on_answer_delta("![a](http://x.com/img.png)")
        session.segment_state.on_reasoning_delta("mid")
        session.segment_state.on_answer_delta("![b](http://y.com/img2.png)")

        resolver = MagicMock()
        resolver.resolve_await = AsyncMock(side_effect=[RuntimeError("timeout"), "ok"])
        session.image_resolver = resolver
        ctrl._sessions["msg_img"] = session

        await ctrl._do_complete_card(session)

        assert resolver.resolve_await.call_count == 2


# ── _on_thinking_segment 集成测试 ──


class TestOnThinking:
    def test_splits_and_dispatches(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._reload = lambda: {"display": {"platforms": {"feishu": {"show_reasoning": True}}}}  # type: ignore[assignment]
        session = _make_session("msg_think")
        ctrl._sessions["msg_think"] = session

        with patch.object(ctrl, "_schedule_flush"):
            ctrl._on_thinking_segment(session, "<thinking>reasoning here</thinking>\nanswer text")

        types = [s.type for s in session.segment_state.segments]
        assert types == ["reasoning", "answer"]

    @pytest.mark.asyncio
    async def test_interim_commentary_does_not_start_tool_panel_answer(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_commentary_panel")
        session.state = SessionState.STREAMING
        session.card_id = "card_commentary_panel"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session

        def tool_panel_actions() -> list[dict]:
            return [
                action
                for call in ctrl._client.cardkit_batch_update.await_args_list
                for action in call.args[1]
                if action.get("params", {}).get("element_id") == TOOL_PANEL_ELEMENT_ID
                or any(
                    element.get("element_id") == TOOL_PANEL_ELEMENT_ID
                    for element in action.get("params", {}).get("elements", [])
                )
            ]

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_tool_update(
                message_id=session.message_id, tool_name="read", status="started", detail="a.py"
            )
        await ctrl._do_flush(session)
        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_tool_update(
                message_id=session.message_id, tool_name="read", status="completed", detail="ok"
            )
        await ctrl._do_flush(session)

        with patch.object(ctrl, "_schedule_flush"):
            ctrl._on_thinking_segment(
                session,
                "commentary one",
                source="interim_commentary",
            )
        await ctrl._do_flush(session)
        first_partials = [
            action for action in tool_panel_actions()
            if action["action"] == "partial_update_element"
        ]
        assert first_partials[-1]["params"]["partial_element"]["expanded"] is True

        with patch.object(ctrl, "_schedule_flush"):
            ctrl._on_thinking_segment(
                session,
                "commentary two",
                source="interim_commentary",
            )
        await ctrl._do_flush(session)

        later_partials = [
            action for action in tool_panel_actions()
            if action["action"] == "partial_update_element"
        ]
        assert len(later_partials) == len(first_partials)
        answer_segments = [
            seg for seg in session.segment_state.segments if seg.type == "answer"
        ]
        assert answer_segments == []
        assert session.interim_preview.text == "commentary two"
        assert any(
            call.args[1] == STREAMING_ELEMENT_ID and call.args[2] == "commentary two"
            for call in ctrl._client.cardkit_stream_element.await_args_list
        )

    def test_split_reasoning_answer_marks_tool_panel_only_on_first_answer(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._reload = lambda: {"display": {"platforms": {"feishu": {"show_reasoning": True}}}}  # type: ignore[assignment]
        session = _make_session("msg_split_answer_panel")

        with patch.object(ctrl, "_schedule_flush"):
            ctrl._on_thinking_segment(
                session,
                "<thinking>reasoning</thinking>\nanswer one",
            )
            first_revision = session.tool_panel.revision
            session.tool_panel.mark_rendered(first_revision, 0)
            ctrl._on_thinking_segment(session, "answer two")

        answer_segments = [
            seg for seg in session.segment_state.segments if seg.type == "answer"
        ]
        assert [seg.text for seg in answer_segments] == ["reasoning\nanswer oneanswer two"]
        assert session.tool_panel.revision == first_revision

    def test_active_card_answer_ignores_sealed_card_history(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_active_card_answer")
        old_answer = Segment("answer", "answer_old")
        old_answer.text = "answer from card one"
        new_tool = Segment("tool", "tool_new")
        session.segment_state.segments = [old_answer, new_tool]
        session.split_index = 1

        assert ctrl._active_card_has_answer(session) is False
        first_revision = session.tool_panel.revision
        assert ctrl._append_answer_segment(session, "answer from card two") is True
        assert ctrl._active_card_has_answer(session) is True
        assert session.tool_panel.revision == first_revision + 1

        assert ctrl._append_answer_segment(session, " more") is True
        assert session.tool_panel.revision == first_revision + 1

    def test_merged_mode_uses_shared_reasoning_ingestion(self) -> None:
        ctrl = _setup_ctrl()
        _configure_merged(ctrl)
        session = _make_session("msg_think_merged")
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            ctrl._on_thinking_segment(session, "<thinking>reasoning</thinking>\nanswer")

        assert [seg.type for seg in session.segment_state.segments] == ["reasoning", "answer"]
        assert session.merged_reasoning.text == "reasoning"
        assert session.merged_reasoning.active_since is None

    def test_merged_thinking_tag_deltas_append_without_replacing(self) -> None:
        ctrl = _setup_ctrl()
        _configure_merged(ctrl)
        session = _make_session("msg_think_deltas")
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            # Streaming thinking-tag callbacks can contain an open tag with
            # the current delta, before the eventual closing tag arrives.
            ctrl._on_thinking_segment(session, "<thinking>A")
            ctrl._on_thinking_segment(session, "<thinking>B")
            ctrl._on_thinking_segment(session, "<thinking>C")

        reasoning_segments = [
            seg for seg in session.segment_state.segments if seg.type == "reasoning"
        ]
        assert [seg.text for seg in reasoning_segments] == ["A", "B", "C"]
        assert session.merged_reasoning.text == "ABC"

    def test_empty_text_no_flush(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_think2")
        ctrl._sessions["msg_think2"] = session

        with patch.object(ctrl, "_schedule_flush") as m:
            ctrl._on_thinking_segment(session, "")
            m.assert_not_called()

    def test_none_segment_state_skips(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_think3")
        session.segment_state = None
        ctrl._sessions["msg_think3"] = session

        ctrl._on_thinking_segment(session, "some text")

    def test_show_reasoning_false_skips_reasoning(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._reload = lambda: {"display": {"platforms": {"feishu": {"show_reasoning": False}}}}  # type: ignore[assignment]
        session = _make_session("msg_noreas")
        ctrl._sessions["msg_noreas"] = session

        with patch.object(ctrl, "_schedule_flush"):
            ctrl._on_thinking_segment(session, "<thinking>secret thoughts</thinking>\nreal answer")

        assert all(s.type == "answer" for s in session.segment_state.segments)

    def test_reasoning_only_with_show_reasoning(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._reload = lambda: {"display": {"platforms": {"feishu": {"show_reasoning": True}}}}  # type: ignore[assignment]
        session = _make_session("msg_ronly")
        ctrl._sessions["msg_ronly"] = session

        with patch.object(ctrl, "_schedule_flush"):
            ctrl._on_thinking_segment(session, "Reasoning:\njust thinking")

        assert len(session.segment_state.segments) == 1
        assert session.segment_state.segments[0].type == "reasoning"


class TestInterimPreview:
    def _added_elements(self, ctrl: StreamCardController) -> list[dict]:
        return [
            element
            for call in ctrl._client.cardkit_batch_update.await_args_list
            for action in call.args[1]
            for element in action.get("params", {}).get("elements", [])
        ]

    @pytest.mark.asyncio
    async def test_commentary_is_replace_only_and_never_an_answer_segment(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_preview_replace")
        session.state = SessionState.STREAMING
        session.card_id = "card_preview_replace"
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_thinking(
                message_id=session.message_id,
                text="Checking repo...",
                source="interim_commentary",
            ) is True
            assert ctrl.on_thinking(
                message_id=session.message_id,
                text="Found handler...",
                source="interim_commentary",
            ) is True

        assert session.interim_preview.text == "Found handler..."
        assert [seg for seg in session.segment_state.segments if seg.type == "answer"] == []

    @pytest.mark.asyncio
    async def test_final_answer_clears_preview_and_ignores_late_commentary(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_preview_final")
        session.state = SessionState.STREAMING
        session.card_id = "card_preview_final"
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_thinking(
                message_id=session.message_id,
                text="Checking...",
                source="interim_commentary",
            ) is True
        await ctrl._do_flush(session)

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_answer(message_id=session.message_id, text="Final") is True
            assert ctrl.on_thinking(
                message_id=session.message_id,
                text="Late commentary",
                source="interim_commentary",
            ) is False

        assert session.interim_preview.final_started is True
        assert session.interim_preview.text == ""
        assert [seg.text for seg in session.segment_state.segments if seg.type == "answer"] == [
            "Final"
        ]
        await ctrl._do_flush(session)

        preview_streams = [
            call.args[2]
            for call in ctrl._client.cardkit_stream_element.await_args_list
            if call.args[1] == STREAMING_ELEMENT_ID
        ]
        assert preview_streams == ["Checking...", " "]

    @pytest.mark.asyncio
    async def test_preview_uses_one_fixed_element_and_preserves_progress_lane(self) -> None:
        ctrl = _setup_ctrl()
        _configure_progress(ctrl, show_tool_use=False)
        session = _make_session("msg_preview_fixed")
        session.state = SessionState.STREAMING
        session.card_id = "card_preview_fixed"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_thinking(
                message_id=session.message_id,
                text="First commentary",
                source="interim_commentary",
            ) is True
        await ctrl._do_flush(session)

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_thinking(
                message_id=session.message_id,
                text="Latest commentary",
                source="interim_commentary",
            ) is True
            assert ctrl.on_long_running_progress(
                message_id=session.message_id,
                elapsed_seconds=180,
                iteration=7,
            ) is True
        await ctrl._do_flush(session)

        preview_adds = [
            element for element in self._added_elements(ctrl)
            if element.get("element_id") == STREAMING_ELEMENT_ID
        ]
        assert len(preview_adds) == 1
        preview_streams = [
            call.args[2]
            for call in ctrl._client.cardkit_stream_element.await_args_list
            if call.args[1] == STREAMING_ELEMENT_ID
        ]
        assert preview_streams == ["First commentary", "Latest commentary"]
        progress_actions = [
            action
            for call in ctrl._client.cardkit_batch_update.await_args_list
            for action in call.args[1]
            if action.get("params", {}).get("element_id") == _LOADING_ELEMENT_ID
        ]
        assert progress_actions[-1]["params"]["partial_element"]["content"] == (
            "Working · 3 min · Round 7"
        )

    @pytest.mark.asyncio
    async def test_commentary_received_during_creation_is_flushed_after_card_ready(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_preview_creating")
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush") as schedule:
            assert ctrl.on_thinking(
                message_id=session.message_id,
                text="Queued while creating",
                source="interim_commentary",
            ) is True
            schedule.reset_mock()
            await ctrl._do_create_card(session)
            schedule.assert_called_once_with(session)

        await ctrl._do_flush(session)
        assert session.interim_preview.created is True
        assert any(
            element.get("element_id") == STREAMING_ELEMENT_ID
            for element in self._added_elements(ctrl)
        )

    @pytest.mark.asyncio
    async def test_completion_fallback_promotes_only_authoritative_answer(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_preview_fallback")
        session.state = SessionState.STREAMING
        session.card_id = "card_preview_fallback"
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            ctrl.on_thinking(
                message_id=session.message_id,
                text="Checking...",
                source="interim_commentary",
            )
            ctrl.on_thinking(
                message_id=session.message_id,
                text="Confirmed...",
                source="interim_commentary",
            )

        ctrl._apply_completion_payload(
            session=session,
            answer="Authoritative final",
            duration=0,
            model="",
            tokens=None,
            context=None,
        )
        assert session.interim_preview.final_started is True
        assert [seg.text for seg in session.segment_state.segments if seg.type == "answer"] == [
            "Authoritative final"
        ]

        await ctrl._do_complete_card(session)
        card = ctrl._client.cardkit_update.await_args.args[1]
        body_text = "\n".join(
            element.get("content", "")
            for element in card["body"]["elements"]
            if element.get("tag") == "markdown"
        )
        assert "Authoritative final" in body_text
        assert "Checking..." not in body_text
        assert "Confirmed..." not in body_text

    @pytest.mark.asyncio
    async def test_split_recreates_live_preview_but_seals_no_commentary(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_preview_split")
        session.state = SessionState.STREAMING
        session.card_id = "card_preview_old"
        session.card_msg_id = "msg_preview_old"
        session.element_count = 2
        session.interim_preview.replace("Latest before split")
        session.interim_preview.created = True
        session.interim_preview.mark_rendered(session.interim_preview.revision)

        assert await ctrl._do_split_card(session, 0, [], set(), {}, []) is True

        sealed_card = ctrl._client.cardkit_update.await_args.args[1]
        assert not any(
            element.get("element_id") == STREAMING_ELEMENT_ID
            for element in sealed_card["body"]["elements"]
        )
        assert session.interim_preview.text == "Latest before split"
        assert session.interim_preview.created is False
        assert session.interim_preview.dirty is True

        await ctrl._do_flush(session)
        assert any(
            element.get("element_id") == STREAMING_ELEMENT_ID
            for element in self._added_elements(ctrl)
        )

    @pytest.mark.asyncio
    async def test_split_seal_without_answer_omits_done_but_keeps_reasoning(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_split_no_answer")
        session.state = SessionState.STREAMING
        session.card_id = "card_split_no_answer"
        session.card_msg_id = "msg_split_no_answer"
        session.segment_state.on_reasoning_delta("sealed reasoning")
        session.segment_state.segments[0].created = True
        session.segment_state.segments[0].dirty = False

        assert await ctrl._do_split_card(session, 1, [], set(), {}, []) is True

        sealed_card = ctrl._client.cardkit_update.await_args.args[1]
        body_text = str(sealed_card["body"]["elements"])
        assert "sealed reasoning" in body_text
        assert "Done" not in body_text
        assert "完成" not in body_text

    def test_whitespace_answer_does_not_start_final_or_clear_preview(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_preview_whitespace")
        session.state = SessionState.STREAMING
        session.card_id = "card_preview_whitespace"
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_thinking(
                message_id=session.message_id,
                text="Checking...",
                source="interim_commentary",
            ) is True
            assert ctrl.on_answer(message_id=session.message_id, text=" ") is True

        assert session.interim_preview.final_started is False
        assert session.interim_preview.text == "Checking..."

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_answer(message_id=session.message_id, text="Final") is True

        assert session.interim_preview.final_started is True
        assert session.interim_preview.text == ""

    def test_completion_fallback_ignores_whitespace_answer_segment(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_completion_whitespace")
        session.state = SessionState.STREAMING
        session.card_id = "card_completion_whitespace"
        ctrl._sessions[session.message_id] = session

        with patch.object(ctrl, "_schedule_flush"):
            assert ctrl.on_thinking(
                message_id=session.message_id,
                text="Checking...",
                source="interim_commentary",
            ) is True
            assert ctrl.on_answer(message_id=session.message_id, text=" ") is True

        ctrl._apply_completion_payload(
            session=session,
            answer="Authoritative final",
            duration=0,
            model="",
            tokens=None,
            context=None,
        )

        assert session.interim_preview.final_started is True
        answer_text = "".join(
            seg.text for seg in session.segment_state.segments if seg.type == "answer"
        )
        assert "Authoritative final" in answer_text
        assert "Checking..." not in answer_text

    @pytest.mark.asyncio
    async def test_missing_preview_element_is_recreated_on_next_flush(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_preview_missing")
        session.state = SessionState.STREAMING
        session.card_id = "card_preview_missing"
        session.element_count = 1
        ctrl._sessions[session.message_id] = session
        with patch.object(ctrl, "_schedule_flush"):
            ctrl.on_thinking(
                message_id=session.message_id,
                text="Recover me",
                source="interim_commentary",
            )

        ctrl._client.cardkit_stream_element = AsyncMock(
            side_effect=[
                FeishuAPIError(
                    "code=300313, msg=not find elementID : streaming_content;",
                    300313,
                ),
                None,
            ]
        )
        await ctrl._do_flush(session)
        assert session.interim_preview.created is False
        assert session.interim_preview.dirty is True
        assert session.element_count == 1

        await ctrl._do_flush(session)
        assert session.interim_preview.created is True
        assert session.interim_preview.dirty is False
        assert len([
            element for element in self._added_elements(ctrl)
            if element.get("element_id") == STREAMING_ELEMENT_ID
        ]) == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal_state", [SessionState.FAILED, SessionState.ABORTED])
    async def test_terminal_error_or_abort_card_has_no_preview(
        self,
        terminal_state: SessionState,
    ) -> None:
        ctrl = _setup_ctrl()
        session = _make_session(f"msg_preview_{terminal_state.value}")
        session.state = SessionState.STREAMING
        session.card_id = f"card_preview_{terminal_state.value}"
        session.interim_preview.replace("Transient commentary")
        if terminal_state == SessionState.FAILED:
            session.mark_failed()
        else:
            session.interim_preview.start_final()
            session.state = terminal_state

        assert await ctrl._do_complete_card(session) is True
        card = ctrl._client.cardkit_update.await_args.args[1]
        body_text = "\n".join(
            element.get("content", "")
            for element in card["body"]["elements"]
            if element.get("tag") == "markdown"
        )
        assert "Transient commentary" not in body_text


class TestCronDeliver:
    @pytest.mark.parametrize(
        ("enabled", "content"),
        [(False, "text"), (True, "")],
        ids=["disabled", "empty-content"],
    )
    def test_returns_false_without_deliverable_content(self, enabled: bool, content: str) -> None:
        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.enabled = enabled
        assert ctrl.on_cron_deliver(chat_id="c1", content=content, loop=MagicMock()) is False

    def test_sends_card_on_success(self) -> None:
        import threading

        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.enabled = True

        mock_client = AsyncMock()
        mock_client.send_card_to_chat.return_value = "msg_123"
        ctrl._client = mock_client
        ctrl._initialized = True

        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        try:
            result = ctrl.on_cron_deliver(chat_id="c1", content="hello", loop=loop)
            assert result is True
            mock_client.send_card_to_chat.assert_called_once()
            args = mock_client.send_card_to_chat.call_args[0]
            assert args[0] == "c1"
            card = args[1]
            assert card["schema"] == "2.0"
            assert "hello" in card["body"]["elements"][0]["content"]
        finally:
            loop.call_soon_threadsafe(loop.stop)

    def test_sends_card_without_gateway_loop(self) -> None:
        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.enabled = True
        ctrl._cfg.feishu_app_id = "app_id"
        ctrl._cfg.feishu_app_secret = "app_secret"
        ctrl._cfg.env_app_id = ""
        ctrl._cfg.env_app_secret = ""

        mock_client = AsyncMock()
        mock_client.send_card_to_chat.return_value = "msg_123"
        with patch("hermes_lark_streaming.controller.FeishuClient", return_value=mock_client):
            result = ctrl.on_cron_deliver(chat_id="c1", content="hello", loop=None)

        assert result is True
        assert ctrl._initialized is True
        mock_client.send_card_to_chat.assert_called_once()

    def test_initializes_once_across_standalone_workers(self) -> None:
        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.feishu_app_id = "app_id"
        ctrl._cfg.feishu_app_secret = "app_secret"
        ctrl._cfg.env_app_id = ""
        ctrl._cfg.env_app_secret = ""
        constructor_entered = threading.Event()
        release_constructor = threading.Event()
        errors: list[BaseException] = []
        mock_client = AsyncMock()

        def slow_client(*args: object, **kwargs: object) -> AsyncMock:
            constructor_entered.set()
            assert release_constructor.wait(timeout=1)
            return mock_client

        def initialize() -> None:
            try:
                asyncio.run(ctrl._ensure_init())
            except BaseException as exc:
                errors.append(exc)

        with patch("hermes_lark_streaming.controller.FeishuClient", side_effect=slow_client) as cls:
            first = threading.Thread(target=initialize)
            second = threading.Thread(target=initialize)
            first.start()
            assert constructor_entered.wait(timeout=1)
            second.start()
            release_constructor.set()
            first.join(timeout=1)
            second.join(timeout=1)

        assert not first.is_alive()
        assert not second.is_alive()
        assert errors == []
        cls.assert_called_once()

    def test_returns_false_on_standalone_send_failure(self) -> None:
        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.enabled = True
        mock_client = AsyncMock()
        mock_client.send_card_to_chat.side_effect = RuntimeError("API error")
        ctrl._client = mock_client
        ctrl._initialized = True

        assert ctrl.on_cron_deliver(chat_id="c1", content="hello", loop=None) is False

    def test_falls_back_when_gateway_loop_is_not_running(self) -> None:
        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.enabled = True
        mock_client = AsyncMock()
        mock_client.send_card_to_chat.return_value = "msg_123"
        ctrl._client = mock_client
        ctrl._initialized = True

        loop = asyncio.new_event_loop()
        try:
            assert ctrl.on_cron_deliver(chat_id="c1", content="hello", loop=loop) is True
            mock_client.send_card_to_chat.assert_called_once()
        finally:
            if not loop.is_closed():
                loop.close()

    def test_returns_false_on_send_failure(self) -> None:
        import threading

        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.enabled = True

        mock_client = AsyncMock()
        mock_client.send_card_to_chat.side_effect = RuntimeError("API error")
        ctrl._client = mock_client
        ctrl._initialized = True

        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        try:
            result = ctrl.on_cron_deliver(chat_id="c1", content="hello", loop=loop)
            assert result is False
        finally:
            loop.call_soon_threadsafe(loop.stop)


class TestBackgroundDeliver:
    @pytest.mark.asyncio
    async def test_sends_cleaned_text_card(self) -> None:
        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.enabled = True

        mock_client = AsyncMock()
        mock_client.send_card_to_chat.return_value = "msg_123"
        ctrl._client = mock_client
        ctrl._initialized = True

        result = await ctrl.on_background_deliver(
            chat_id="c1",
            preview="prompt",
            content="Here\n\nDone",
            reply_to_message_id="om_1",
        )

        assert result is True
        mock_client.upload_image.assert_not_called()
        mock_client.send_card_to_chat.assert_awaited_once()
        args, kwargs = mock_client.send_card_to_chat.call_args
        assert args[0] == "c1"
        assert kwargs["reply_to_message_id"] == "om_1"
        card = args[1]
        body = card["body"]["elements"][0]["content"]
        assert "Here\n\nDone" in body

    @pytest.mark.asyncio
    async def test_returns_false_on_empty_cleaned_text(self) -> None:
        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.enabled = True

        mock_client = AsyncMock()
        mock_client.send_card_to_chat.return_value = "msg_123"
        ctrl._client = mock_client
        ctrl._initialized = True

        result = await ctrl.on_background_deliver(
            chat_id="c1",
            preview="prompt",
            content="",
        )

        assert result is False
        mock_client.upload_image.assert_not_called()
        mock_client.send_card_to_chat.assert_not_called()
