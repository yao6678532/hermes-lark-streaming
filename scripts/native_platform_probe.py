"""Offline real-host platform discovery, producer and CardKit integration checks."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch


def run_platform_probe() -> dict[str, Any]:
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import get_plugin_manager
    from hermes_constants import get_hermes_home

    plugin = Path(__file__).resolve().parent.parent
    home = Path(get_hermes_home())
    directory = home / "plugins"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "hermes-lark-streaming").symlink_to(plugin, target_is_directory=True)
    provider = directory / "audit-offline-model"
    provider.mkdir()
    (provider / "plugin.yaml").write_text('name: audit-offline-model\nversion: "0.0.1"\nkind: standalone\n')
    (provider / "__init__.py").write_text("""
REPLIES = []
def respond(**kwargs):
    if not REPLIES:
        raise SystemExit("Offline provider exhausted")
    return REPLIES.pop(0)
def register(ctx):
    ctx.register_middleware("llm_execution", respond)
""")
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [platforms/feishu, hermes-lark-streaming, audit-offline-model]\n"
        "  hook_callback_timeout: 5\n  stream_reasoning_deltas: true\n"
        "streaming:\n  enabled: true\nfeishu:\n  app_id: offline-app\n  app_secret: offline-secret\n"
        "display:\n  platforms:\n    feishu:\n      show_reasoning: true\n"
    )
    os.environ["FEISHU_APP_ID"] = "offline-app"
    os.environ["FEISHU_APP_SECRET"] = "offline-secret"
    # Real gateway startup imports its facade before asking for the adapter.
    # Keep the default deadline workers: registering must not wait on this
    # caller's gateway.run import lock.
    from gateway.run import GatewayRunner

    assert callable(GatewayRunner)
    manager = get_plugin_manager()
    manager.discover_and_load()
    loaded = manager._plugins.get("hermes-lark-streaming")
    assert loaded is not None and loaded.enabled and not loaded.error, getattr(loaded, "error", None)
    entry = platform_registry.get("feishu")
    assert entry is not None and entry.adapter_factory.__name__ == "FeishuStreamingAdapter", repr(entry)
    provider_module = manager._plugins["audit-offline-model"].module
    assert provider_module is not None
    checks = asyncio.run(exercise(entry.adapter_factory, provider_module))
    checks["gateway_first_import_with_default_threaded_plugin_loader"] = True
    from hermes_cli.config import read_raw_config, save_config

    from hermes_lark_streaming.migration import prepare_config

    original = read_raw_config()
    updated, _changes = prepare_config(original)
    save_config(updated, strip_defaults=False)
    migrated = read_raw_config()
    assert migrated["feishu"] == original["feishu"]
    assert migrated["display"] == original["display"]
    assert prepare_config(migrated)[1] == []
    checks["real_host_config_save_preserves_design_and_is_idempotent"] = True
    manager.unload("hermes-lark-streaming")
    restored = platform_registry.get("feishu")
    assert restored is not None and restored.adapter_factory.__name__ == "FeishuAdapter"
    checks["disable_restores_builtin_adapter"] = True
    return {"checks": checks, "check_count": len(checks), "all_checks_passed": all(checks.values())}


async def exercise(factory: Any, provider: Any) -> dict[str, bool]:
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent, ProcessingOutcome
    from gateway.session import SessionSource
    from gateway.session_context import clear_session_vars, set_session_vars
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
    from openai.types.chat import ChatCompletion
    from run_agent import AIAgent

    from hermes_lark_streaming.agent_status import conversation_key
    from hermes_lark_streaming.feishu import FeishuClient

    adapter = factory(PlatformConfig(enabled=True, extra={"app_id": "offline-app", "app_secret": "offline-secret"}))
    client = MagicMock(spec=FeishuClient)
    counter = itertools.count()
    for name in (
        "cardkit_create",
        "reply_card_by_id",
        "send_card_to_chat",
        "cardkit_batch_update",
        "cardkit_update",
        "cardkit_stream_element",
        "cardkit_close_streaming",
        "send_text_to_chat",
        "send_card_id_to_chat",
    ):
        setattr(client, name, AsyncMock())
    client.cardkit_create.side_effect = lambda *_a, **_kw: f"offline-card-{next(counter)}"
    client.reply_card_by_id.side_effect = lambda *_a, **_kw: f"offline-message-{next(counter)}"
    client.send_card_id_to_chat.side_effect = lambda *_a, **_kw: f"offline-static-{next(counter)}"
    adapter.cards._client = client
    source = SessionSource(platform=Platform.FEISHU, chat_id="native-chat", user_id="native-owner")
    event = MessageEvent(text="Offline native turn", message_id="native-inbound", source=source)
    key = adapter._event_session_key(event)
    await adapter.on_processing_start(event)
    tokens = set_session_vars(
        platform="feishu",
        chat_id=source.chat_id,
        session_key=key,
        session_id="native-conversation",
        message_id=event.message_id,
    )
    agent = AIAgent(
        model="gpt-4.1-mini",
        provider="openai",
        api_mode="chat_completions",
        base_url="https://offline.invalid/v1",
        api_key="offline-no-network",
        enabled_toolsets=["memory"],
        max_iterations=3,
        quiet_mode=True,
        skip_context_files=True,
        skip_background_review=True,
        session_id="native-conversation",
        platform="feishu",
    )
    consumer = GatewayStreamConsumer(
        adapter,
        source.chat_id,
        StreamConsumerConfig(edit_interval=0.01, cursor=""),
        initial_reply_to_id=event.message_id,
    )
    agent.stream_delta_callback = consumer.on_delta
    agent.interim_assistant_callback = lambda text, already_streamed=False: (
        consumer.on_segment_break() if already_streamed else consumer.on_commentary(text)
    )
    call = {
        "id": "native-memory",
        "type": "function",
        "function": {
            "name": "memory",
            "arguments": json.dumps(
                {
                    "action": "add",
                    "target": "memory",
                    "content": "offline foreground fact",
                }
            ),
        },
    }
    for message, finish in (
        (
            {
                "role": "assistant",
                "content": "First preamble.",
                "reasoning_content": "First reasoning.",
                "tool_calls": [call],
            },
            "tool_calls",
        ),
        ({"role": "assistant", "content": "Final native answer.", "reasoning_content": "Second reasoning."}, "stop"),
    ):
        provider.REPLIES.append(
            ChatCompletion.model_validate(
                {
                    "id": "native-local-response",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "gpt-4.1-mini",
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 25, "completion_tokens": 10, "total_tokens": 35},
                }
            )
        )
    task = asyncio.create_task(consumer.run())
    try:
        result = await asyncio.to_thread(agent.run_conversation, "Offline native turn")
        assert result["final_response"] == "Final native answer.", result
        consumer.finish(result["final_response"])
        await asyncio.wait_for(task, 15)
        if not consumer.delivered_final_matches(result["final_response"]):
            delivered = await adapter.send(
                source.chat_id,
                result["final_response"],
                reply_to=event.message_id,
                metadata={"notify": True},
            )
            assert delivered.success
        from agent.display import get_tool_emoji
        from agent.i18n import t
        from gateway.platforms.base import SendResult
        from plugins.platforms.feishu.adapter import FeishuAdapter

        native = AsyncMock(return_value=SendResult(success=True, message_id="native-notice"))
        with patch.object(FeishuAdapter, "send", new=native):
            tool_line = t("gateway.progress.tool_pending", emoji=get_tool_emoji("memory"), tool="memory")
            await adapter.send(source.chat_id, tool_line)
            native.assert_not_awaited()
            await adapter.send(source.chat_id, "Native operational notice")
            await adapter.send_or_update_status(source.chat_id, "run:retry", "Native API retry status")
            assert native.await_count == 2
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        ref = adapter.cards._agent_status_manager.registry.get(conversation_key(source.chat_id))
        assert ref is not None and ref.finalized and ref.card_snapshot, "No completed native card"
        card = json.dumps(ref.card_snapshot, ensure_ascii=False)
        assert "Final native answer." in card, card
        assert "First preamble." not in card, "Replace-only interim preview leaked into the final answer"
        assert "First reasoning." in card and "Second reasoning." in card, card
        assert "memory" in card, card
        assert client.cardkit_create.await_count == 1, client.cardkit_create.await_count
        client.send_text_to_chat.assert_not_awaited()
        checks = {
            "real_plugin_discovery_and_feishu_override": True,
            "actual_agent_tool_loop_to_existing_controller": True,
            "host_consumer_final_snapshot_reaches_final_card": True,
            "authoritative_reasoning_and_tool_panel_preserved": True,
            "api_boundaries_do_not_seal_or_duplicate_card": True,
            "interim_preview_keeps_existing_replace_only_design": True,
            "known_tool_chrome_is_suppressed_without_dropping_native_notices": True,
        }
        checks.update(await boundary_checks(adapter, client))
        checks.update(await clarify_and_static_checks(adapter, client))
        checks.update(await approval_checks(adapter))
        checks.update(await profile_isolation_checks(adapter, client))
        checks.update(await review_delivery_checks(adapter, client, provider, agent, key))
        checks.update(await stability_checks(adapter, client))
        return checks
    finally:
        clear_session_vars(tokens)
        agent.release_clients()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for item in list(adapter.card_tasks):
            item.cancel()
        if adapter.card_tasks:
            await asyncio.gather(*adapter.card_tasks, return_exceptions=True)


async def boundary_checks(adapter: Any, client: Any) -> dict[str, bool]:
    from gateway.config import Platform
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent, ProcessingOutcome
    from gateway.run_turn_followup_ack import _run_followup_processing_hook
    from gateway.session import SessionSource
    from gateway.session_context import clear_session_vars, set_session_vars
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
    from hermes_cli.lifecycle import invoke_hook
    from plugins.platforms.feishu.adapter import FeishuAdapter

    source = SessionSource(platform=Platform.FEISHU, chat_id="boundary-chat", user_id="boundary-owner")
    checks: dict[str, bool] = {}

    async def start(ident: str) -> tuple[Any, str, Any]:
        event = MessageEvent(text="Offline boundary", message_id=ident, source=source)
        await _run_followup_processing_hook(adapter, event, "on_processing_start")
        key = adapter._event_session_key(event)
        tokens = set_session_vars(
            platform="feishu",
            chat_id=source.chat_id,
            session_key=key,
            session_id=f"physical-{ident}",
            message_id=ident,
        )
        invoke_hook("on_session_start", session_id=f"physical-{ident}", platform="feishu", model="audit")
        invoke_hook(
            "pre_api_request",
            session_id=f"physical-{ident}",
            turn_id=f"turn-{ident}",
            api_call_count=1,
            platform="feishu",
            model="audit",
        )
        await adapter.bridge.drain()
        return event, key, tokens

    first, key, token = await start("queued-first")
    sent = await adapter.send(source.chat_id, "First queued answer", metadata={"expect_edits": True})
    invoke_hook("on_session_end", session_id="physical-queued-first", turn_id="turn-queued-first", completed=True)
    await adapter.bridge.drain()
    first_turn = adapter.bridge.messages[first.message_id]
    assert not first_turn.completed
    clear_session_vars(token)
    second, _key, token = await start("queued-second")
    assert first_turn.completed, "Follow-up admission did not finalize preceding card"
    second_turn = adapter.bridge.messages[second.message_id]
    # Delayed old foreground completion must not bind or seal the new card.
    invoke_hook("on_session_end", session_id="physical-queued-first", turn_id="turn-queued-first", interrupted=True)
    await adapter.bridge.drain()
    assert not second_turn.terminal and not second_turn.completed
    await adapter.send(source.chat_id, "Second queued answer", metadata={"notify": True})
    await _run_followup_processing_hook(adapter, second, "on_processing_complete", ProcessingOutcome.SUCCESS)
    assert second_turn.completed and not second_turn.interrupted
    checks["real_followup_hooks_finalize_each_inbound_separately"] = True
    checks["late_old_completion_cannot_seal_followup"] = True
    assert (await adapter.edit_message(source.chat_id, sent.message_id, "STALE", finalize=True)).success
    clear_session_vars(token)

    stopped, key, token = await start("stopped")
    await adapter.send(source.chat_id, "Partial stopped answer", metadata={"expect_edits": True})
    stopped_turn = adapter.bridge.messages[stopped.message_id]
    invoke_hook(
        "agent_loop_stopped",
        session_key=key,
        platform="feishu",
        reason="User requested stop",
        invalidation_reason="stop_command",
    )
    await adapter.bridge.drain()
    assert stopped_turn.completed and stopped_turn.interrupted and stopped_turn.stop_command
    checks["official_stop_hook_seals_card_and_preserves_continue_hint"] = True
    clear_session_vars(token)

    long_event, _key, token = await start("long-card-answer")
    body = "native-long-answer-" + "x" * 12000 + "-end"
    consumer = GatewayStreamConsumer(adapter, source.chat_id, StreamConsumerConfig(edit_interval=0.01, cursor=""))
    consumer_task = asyncio.create_task(consumer.run())
    consumer.on_delta(body[:4500])
    await asyncio.sleep(0.02)
    consumer.on_delta(body[4500:])
    invoke_hook(
        "on_session_end", session_id="physical-long-card-answer", turn_id="turn-long-card-answer", completed=True
    )
    consumer.finish(body)
    await asyncio.wait_for(consumer_task, 15)
    assert consumer.delivered_final_matches(body)
    await adapter.on_processing_complete(long_event, ProcessingOutcome.SUCCESS)
    from hermes_lark_streaming.agent_status import conversation_key

    long_ref = adapter.cards._agent_status_manager.registry.get(conversation_key(source.chat_id))
    rendered = "".join(
        element.get("content", "")
        for element in long_ref.card_snapshot["body"]["elements"]
        if element.get("tag") == "markdown"
    )
    assert rendered == body, "Host plaintext segmentation truncated or repeated the CardKit answer"
    checks["host_plaintext_limit_does_not_truncate_long_card_answer"] = True
    clear_session_vars(token)

    original_create = client.cardkit_create.side_effect
    client.cardkit_create.side_effect = RuntimeError("Offline CardKit create failure")
    fallback = AsyncMock(return_value=SendResult(success=True, message_id="native-text"))
    with patch.object(FeishuAdapter, "send", new=fallback) as native:
        failed, _key, token = await start("creation-failed")
        result = await adapter.send(source.chat_id, "Final creation fallback", metadata={"notify": True})
        assert result.success
        await adapter.on_processing_complete(failed, ProcessingOutcome.SUCCESS)
        assert native.await_count == 1
        assert native.await_args.args[1] == "Final creation fallback"
        clear_session_vars(token)
    client.cardkit_create.side_effect = original_create
    checks["card_creation_failure_uses_native_final_once"] = True

    failed, _key, token = await start("update-failed")
    await adapter.send(source.chat_id, "Final update fallback", metadata={"notify": True})
    client.cardkit_update.side_effect = RuntimeError("Offline CardKit final update failure")
    fallback = AsyncMock(return_value=SendResult(success=True, message_id="native-text"))
    with patch.object(FeishuAdapter, "send", new=fallback) as native:
        await adapter.on_processing_complete(failed, ProcessingOutcome.SUCCESS)
        assert native.await_count == 1
        assert native.await_args.args[1] == "Final update fallback"
    client.cardkit_update.side_effect = None
    clear_session_vars(token)
    checks["final_card_update_failure_uses_native_final_once"] = True
    first, _key, token = await start("queue-fallback-first")
    await adapter.send(source.chat_id, "Queued first fallback", metadata={"notify": True})
    invoke_hook(
        "on_session_end",
        session_id="physical-queue-fallback-first",
        turn_id="turn-queue-fallback-first",
        completed=True,
    )
    await adapter.bridge.drain()
    clear_session_vars(token)
    client.cardkit_update.side_effect = RuntimeError("Offline queued boundary update failure")
    fallback = AsyncMock(return_value=SendResult(success=True, message_id="native-queued-text"))
    with patch.object(FeishuAdapter, "send", new=fallback) as native:
        second, _key, token = await start("queue-fallback-second")
        assert native.await_count == 1 and native.await_args.args[1] == "Queued first fallback"
        client.cardkit_update.side_effect = None
        await adapter.send(source.chat_id, "Queued second answer", metadata={"notify": True})
        await adapter.on_processing_complete(second, ProcessingOutcome.SUCCESS)
        await adapter.on_processing_complete(first, ProcessingOutcome.SUCCESS)
        assert native.await_count == 1, "Outer queued cleanup repeated the earlier fallback"
    clear_session_vars(token)
    checks["queued_handoff_card_failure_preserves_previous_reply_once"] = True
    return checks


async def review_delivery_checks(adapter: Any, client: Any, provider: Any, parent: Any, key: str) -> dict[str, bool]:
    from agent.background_review import _run_review_in_thread, summarize_background_review_actions
    from agent.i18n import t
    from gateway.session_context import set_session_vars
    from openai.types.chat import ChatCompletion

    def queue_review(target: str) -> None:
        call = {
            "id": "integrated-review-" + target,
            "type": "function",
            "function": {
                "name": "memory",
                "arguments": json.dumps(
                    {
                        "action": "add",
                        "target": target,
                        "content": "native review fact " + target,
                    }
                ),
            },
        }
        for message, finish in (
            ({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"),
            ({"role": "assistant", "content": "Done."}, "stop"),
        ):
            provider.REPLIES.append(
                ChatCompletion.model_validate(
                    {
                        "id": "native-review-response",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "gpt-4.1-mini",
                        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 25, "completion_tokens": 10, "total_tokens": 35},
                    }
                )
            )

    # Keep delivery paused while installing transport failures. Capture and
    # real producer dispatch stay active; only the debounce timer is held.
    # Hermes session vars are deliberately not nestable: boundary scenarios
    # cleared them, so bind the real parent chat again before the fork.
    set_session_vars(
        platform="feishu",
        chat_id="native-chat",
        session_key=key,
        session_id="native-conversation",
        message_id="native-inbound",
    )
    with patch.object(adapter, "schedule_reviews"):
        for target in ("memory", "user"):
            queue_review(target)
            await asyncio.to_thread(_run_review_in_thread, parent, [], "Save one memory fact.", {}, None, True)
    batch = adapter.reviews.buffer.snapshot(adapter.cards._profile_home, "native-chat")
    assert len(batch.receipts) == 2, "Native registered observer did not persist both actual review writes"
    client.cardkit_batch_update.side_effect = RuntimeError("Offline card failure")
    client.send_text_to_chat.side_effect = RuntimeError("Offline fallback failure")
    assert not await adapter.reviews.deliver("native-chat")
    assert len(adapter.reviews.buffer.snapshot(adapter.cards._profile_home, "native-chat").receipts) == 2
    client.cardkit_batch_update.side_effect = None
    client.send_text_to_chat.side_effect = None
    assert await adapter.reviews.deliver("native-chat")
    assert not adapter.reviews.buffer.snapshot(adapter.cards._profile_home, "native-chat").receipts
    from hermes_lark_streaming.agent_status import conversation_key

    ref = adapter.cards._agent_status_manager.registry.get(conversation_key("native-chat"))
    assert ref is not None and "Memory updated" in ref.agent_status
    merged = ref.agent_status
    calls = client.cardkit_batch_update.await_count
    for receipt in batch.receipts:
        single = type(batch)((receipt,)).summarize(summarize_background_review_actions)
        text = t("display.review.summary_callback", summary=" · ".join(single))
        await adapter.send("native-chat", text, metadata={"_interim_send": True})
    assert client.cardkit_batch_update.await_count == calls
    assert ref.agent_status == merged and "User profile updated" in merged
    return {
        "registered_native_review_observer_persists_real_fork_write": True,
        "failed_card_and_fallback_keep_pending_receipt": True,
        "confirmed_card_update_acknowledges_exact_receipt_batch": True,
        "two_actual_reviews_merge_without_native_callback_overwriting_or_redelivery": True,
    }


async def clarify_and_static_checks(adapter: Any, client: Any) -> dict[str, bool]:

    from agent.i18n import t
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent, ProcessingOutcome
    from gateway.session import SessionSource
    from gateway.session_context import clear_session_vars, set_session_vars
    from hermes_cli.lifecycle import invoke_hook
    from tools import clarify_gateway
    from tools.human_input_hooks import human_input_request

    config_path = adapter.cards._profile_home / "config.yaml"
    original = config_path.read_text()
    raw = adapter.cards._cfg._reload()
    raw["display"]["platforms"]["feishu"]["clarify_style"] = "card"
    raw["streaming"]["progress_mode"] = "card"
    config_path.write_text(json.dumps(raw))
    adapter.cards._cfg._raw = raw
    source = SessionSource(platform=Platform.FEISHU, chat_id="clarify-chat", user_id="clarify-owner")
    event = MessageEvent(text="Offline clarify", message_id="clarify-inbound", source=source)
    await adapter.on_processing_start(event)
    key = adapter._event_session_key(event)
    tokens = set_session_vars(
        platform="feishu",
        chat_id=source.chat_id,
        session_key=key,
        session_id="clarify-physical",
        message_id=event.message_id,
    )
    try:
        invoke_hook("on_session_start", session_id="clarify-physical", model="audit", platform="feishu")
        invoke_hook("pre_api_request", session_id="clarify-physical", turn_id="clarify-turn", api_call_count=1)
        await adapter.bridge.drain()
        session = adapter.cards._sessions[event.message_id]
        heartbeat = t("gateway.progress.working_heartbeat", minutes=3, detail="")
        await adapter.send(source.chat_id, heartbeat, metadata={"_interim_send": True})
        assert session.progress.visible and session.progress.elapsed_seconds == 180
        assert session.progress.iteration == 1
        await adapter.send(source.chat_id, "Before clarification", metadata={"expect_edits": True})
        original_card = session.card_id
        invoke_hook(
            "pre_tool_call", tool_name="clarify", args={}, session_id="clarify-physical", turn_id="clarify-turn"
        )
        choices = ["staging **safe**", "production `blue`"]
        clarify_gateway.register("native-clarify", key, "Choose targets", choices, multi_select=True)
        with human_input_request("clarify", session_key=key) as request:
            await adapter.bridge.drain()
            assert session.state.value == "clarify_paused"
            sent = await adapter.send_clarify(source.chat_id, "Choose targets", choices, "native-clarify", key)
            assert sent.success
            callback = {
                "event": {
                    "action": {
                        "value": {"hermes_lark_action": "clarify_multi_submit", "clarify_id": "native-clarify"},
                        "form_value": {"clarify_multi_select": ["0", "1"]},
                    },
                    "context": {"open_chat_id": source.chat_id},
                    "operator": {"open_id": "clarify-owner"},
                }
            }
            assert await adapter.cards.on_clarify_action(
                raw_message=callback, source_chat_id=source.chat_id, session_key=key
            )
            response = clarify_gateway.wait_for_response("native-clarify", timeout=0)
            assert json.loads(response) == choices
            request.outcome = "submitted"
        invoke_hook(
            "post_tool_call",
            tool_name="clarify",
            args={},
            result='{"success": true}',
            session_id="clarify-physical",
            turn_id="clarify-turn",
            status="ok",
        )
        await adapter.bridge.drain()
        assert session.card_id != original_card, "Clarify completion did not split and resume existing card session"
        invoke_hook("pre_api_request", session_id="clarify-physical", turn_id="clarify-turn", api_call_count=2)
        await adapter.send(source.chat_id, "After clarification", metadata={"notify": True})
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

        # These deliveries must stay independent even when a processing-turn
        # ContextVar remains bound to the foreground chat.
        cron = await adapter.send(
            source.chat_id, "Online cron body", metadata={"job_id": "audit-job", "thread_id": "topic"}
        )
        assert cron.success
        assert "Online cron body" in json.dumps(client.send_card_to_chat.await_args.args[1])
        background = t("gateway.background.complete_header", preview="audit background") + "Background body"
        result = await adapter.send(source.chat_id, background)
        assert result.success
        assert "Background body" in json.dumps(client.send_card_to_chat.await_args.args[1])
        from hermes_lark_streaming.native_adapter import standalone_send

        media_sender = AsyncMock(return_value={"success": True, "message_id": "media-id"})
        with (
            patch("hermes_lark_streaming.native_adapter.FeishuClient", return_value=client),
            patch(
                "hermes_lark_streaming.native_adapter.builtin._standalone_send",
                new=media_sender,
            ),
        ):
            standalone = await standalone_send(
                adapter.config,
                source.chat_id,
                "Offline cron body",
                thread_id="topic",
                media_files=[("fake.png", False)],
            )
        assert standalone["success"]
        assert media_sender.await_args.args[2] == "", "Offline cron repeated its text while delivering media"
        assert media_sender.await_count == 1
        return {
            "clarify_v2_native_pending_and_canonical_multiselect_answer": True,
            "clarify_wait_pauses_then_splits_and_resumes_existing_controller": True,
            "online_cron_and_background_cards_remain_independent": True,
            "offline_cron_sends_card_and_media_without_duplicate_text": True,
            "native_heartbeat_preserves_card_progress_elapsed_and_iteration": True,
        }
    finally:
        clear_session_vars(tokens)
        config_path.write_text(original)
        adapter.cards._cfg._raw = None


async def approval_checks(adapter: Any) -> dict[str, bool]:
    from types import SimpleNamespace

    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import approval
    from tools.approval_gateway_wait import _ApprovalEntry

    config_path = adapter.cards._profile_home / "config.yaml"
    original = config_path.read_text()
    config = adapter.cards._cfg._reload()
    config["display"]["platforms"]["feishu"]["confirmation_style"] = "openclaw"
    config_path.write_text(json.dumps(config))
    key, chat = "approval-native-session", "approval-native-chat"
    tokens = set_session_vars(
        platform="feishu",
        chat_id=chat,
        session_key=key,
        session_id="approval-physical",
        user_id="approval-owner",
        chat_type="dm",
    )
    first = _ApprovalEntry({"command": "echo first"})
    second = _ApprovalEntry({"command": "echo second"})
    approval._gateway_queues[key] = [first, second]
    response = SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="native-approval-message"))
    network = AsyncMock(return_value=response)
    callback = SimpleNamespace(
        context=SimpleNamespace(open_chat_id=chat),
        operator=SimpleNamespace(open_id="approval-owner", user_id="", union_id=""),
    )
    old_auth = adapter._authorization_check
    adapter.set_authorization_check(lambda user, *_args: user == "approval-owner")
    try:
        with patch.object(adapter, "_client", object()), patch.object(adapter, "_feishu_send_with_retry", new=network):
            for entry in (first, second):
                sent = await adapter.send_exec_approval(
                    chat, entry.data["command"], key, request_id=entry.data["request_id"]
                )
                assert sent.success
        ids = list(adapter._approval_state)
        assert len(ids) == 2
        first_id, second_id = ids
        first_payload = {"hermes_action": "approve_once", "approval_id": first_id}
        adapter._handle_approval_card_action(
            event=callback,
            action_value={"hermes_action": "approve_once", "approval_id": second_id},
            loop=asyncio.get_running_loop(),
        )
        assert not first.event.is_set() and not second.event.is_set(), "Out-of-order approval settled a waiter"
        callback.operator.open_id = "unauthorized"
        adapter._handle_approval_card_action(
            event=callback, action_value=first_payload, loop=asyncio.get_running_loop()
        )
        assert not first.event.is_set()
        callback.operator.open_id = "approval-owner"
        adapter._handle_approval_card_action(
            event=callback, action_value=first_payload, loop=asyncio.get_running_loop()
        )
        assert first.event.is_set() and first.result == "once" and not second.event.is_set()
        adapter._handle_approval_card_action(
            event=callback, action_value=first_payload, loop=asyncio.get_running_loop()
        )
        assert not second.event.is_set(), "Duplicate click approved the next request"

        # Expire this card's own request, leaving a different real request
        # pending. The callback must not use the legacy unkeyed FIFO resolver.
        replacement = _ApprovalEntry({"command": "echo replacement"})
        approval._gateway_queues[key] = [replacement]
        adapter._handle_approval_card_action(
            event=callback,
            action_value={"hermes_action": "approve_once", "approval_id": second_id},
            loop=asyncio.get_running_loop(),
        )
        assert not replacement.event.is_set()
        assert "echo first" in network.await_args_list[0].kwargs["payload"]
        return {
            "native_approval_cards_keep_existing_design_and_official_payloads": True,
            "native_operator_checks_and_fifo_duplicate_guards_remain_active": True,
            "expired_approval_card_cannot_approve_another_request": True,
        }
    finally:
        approval._gateway_queues.pop(key, None)
        adapter.set_authorization_check(old_auth)
        clear_session_vars(tokens)
        config_path.write_text(original)


async def profile_isolation_checks(primary: Any, primary_client: Any) -> dict[str, bool]:
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from gateway.config import Platform, PlatformConfig
    from gateway.platform_registry import platform_registry
    from gateway.platforms.event import MessageEvent, ProcessingOutcome
    from gateway.session import SessionSource
    from gateway.session_context import clear_session_vars, set_session_vars
    from hermes_cli.lifecycle import invoke_hook
    from hermes_cli.plugins import get_plugin_manager
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    from hermes_lark_streaming.agent_status import conversation_key
    from hermes_lark_streaming.feishu import FeishuClient

    home = primary.cards._profile_home / "profiles/native-b"
    home.mkdir(parents=True)
    (home / "plugins").mkdir()
    (home / "plugins/hermes-lark-streaming").symlink_to(
        Path(__file__).resolve().parent.parent, target_is_directory=True
    )
    config = json.loads(json.dumps(primary.cards._cfg._reload()))
    config["plugins"]["enabled"] = ["platforms/feishu", "hermes-lark-streaming"]
    config["feishu"] = {"app_id": "offline-b", "app_secret": "offline-secret-b"}
    (home / "config.yaml").write_text(json.dumps(config))
    token = set_hermes_home_override(home)
    secret_token = set_secret_scope(build_profile_secret_scope(home))
    try:
        manager = get_plugin_manager()
        manager.discover_and_load()
        entry = platform_registry.get("feishu")
        assert entry is not None and entry.adapter_factory.__name__ == "FeishuStreamingAdapter"
        secondary = entry.adapter_factory(
            PlatformConfig(
                enabled=True,
                extra={"app_id": "offline-b", "app_secret": "offline-secret-b"},
            )
        )
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(token)
    assert secondary.cards is not primary.cards
    assert secondary._app_id == "offline-b"
    client = MagicMock(spec=FeishuClient)
    for name in (
        "cardkit_create",
        "reply_card_by_id",
        "cardkit_update",
        "cardkit_batch_update",
        "cardkit_close_streaming",
        "cardkit_stream_element",
        "send_text_to_chat",
    ):
        setattr(client, name, AsyncMock())
    client.cardkit_create.return_value = "profile-b-card"
    client.reply_card_by_id.return_value = "profile-b-message"
    secondary.cards._client = client

    async def run(adapter: Any, text: str) -> None:
        home_token = set_hermes_home_override(adapter.cards._profile_home)
        secret_token = set_secret_scope(build_profile_secret_scope(adapter.cards._profile_home))
        source = SessionSource(platform=Platform.FEISHU, chat_id="same-profile-chat", user_id="same-owner")
        event = MessageEvent(text="Profile isolation", source=source, message_id="same-profile-message")
        session_tokens = None
        try:
            await adapter.on_processing_start(event)
            key = adapter._event_session_key(event)
            session_tokens = set_session_vars(
                platform="feishu",
                chat_id=source.chat_id,
                session_key=key,
                session_id="same-physical",
                message_id=event.message_id,
            )
            invoke_hook("on_session_start", session_id="same-physical", platform="feishu", model="audit")
            invoke_hook("pre_api_request", session_id="same-physical", turn_id="same-turn", api_call_count=1)
            await adapter.bridge.drain()
            await adapter.send(source.chat_id, text, metadata={"expect_edits": True})
            await asyncio.sleep(0)
            invoke_hook("on_session_end", session_id="same-physical", turn_id="same-turn", completed=True)
            await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
            ref = adapter.cards._agent_status_manager.registry.get(conversation_key(source.chat_id))
            assert ref is not None and text in json.dumps(ref.card_snapshot)
        finally:
            if session_tokens is not None:
                clear_session_vars(session_tokens)
            reset_secret_scope(secret_token)
            reset_hermes_home_override(home_token)

    await asyncio.gather(run(primary, "PRIMARY isolated answer"), run(secondary, "SECONDARY isolated answer"))
    a = primary.cards._agent_status_manager.registry.get(conversation_key("same-profile-chat"))
    b = secondary.cards._agent_status_manager.registry.get(conversation_key("same-profile-chat"))
    assert "SECONDARY isolated answer" not in json.dumps(a.card_snapshot)
    assert "PRIMARY isolated answer" not in json.dumps(b.card_snapshot)
    client.send_text_to_chat.assert_not_awaited()
    assert primary.cards._client is primary_client
    return {
        "real_registered_adapters_isolate_identical_chat_message_session_and_turn_ids": True,
        "secondary_profile_uses_resolved_own_credentials_and_controller": True,
    }


async def stability_checks(adapter: Any, client: Any) -> dict[str, bool]:
    """Actual inherited interactive methods, shutdown and ownership after retirement."""
    import time

    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent, ProcessingOutcome
    from gateway.session import SessionSource
    from plugins.platforms.feishu.adapter import FeishuAdapter

    from hermes_lark_streaming.streaming.session import SessionState

    source = SessionSource(platform=Platform.FEISHU, chat_id="command-chat", user_id="command-owner")
    event = MessageEvent(text="/status", message_id="command-inbound", source=source)
    await adapter.on_processing_start(event)
    result = await adapter.send(source.chat_id, "Ordinary command response", metadata={"notify": True})
    turn = adapter.bridge.messages[event.message_id]
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert result.success and turn.completed
    late = await adapter.edit_message(source.chat_id, result.message_id, "old edit", finalize=True)
    assert late.success and result.message_id not in adapter._card_messages
    with patch.object(FeishuAdapter, "edit_message", new=AsyncMock()) as edit:
        unknown = await adapter.edit_message(source.chat_id, "unknown-old-card", "late")
        cross = await adapter.edit_message("another-chat", result.message_id, "late")
        assert not unknown.success and not cross.success and not edit.called
    adapter._client = object()
    network = AsyncMock(
        return_value=type(
            "Response",
            (),
            {"code": 0, "success": lambda _self: True, "data": type("Data", (), {"message_id": "official-card"})()},
        )()
    )
    with patch.object(adapter, "_feishu_send_with_retry", new=network):
        picker = await adapter.send_choice_picker(
            source.chat_id, "Choose", [{"label": "Yes", "value": "yes"}], "picker-session", lambda _: None
        )
        prompt = await adapter.send_update_prompt(source.chat_id, "Install update?", session_key="update-session")
    assert picker.success and prompt.success
    payloads = [json.loads(call.kwargs["payload"]) for call in network.await_args_list]
    assert "hermes_choice_picker_action" in json.dumps(payloads[0])
    assert "hermes_update_prompt_action" in json.dumps(payloads[1])
    assert "hermes_lark_action" not in json.dumps(payloads)
    assert adapter._choice_picker_state and adapter._update_prompt_state

    # Three independent cards: one hangs, one fails, the other still closes.
    turns = []
    ids = []
    for i in range(3):
        src = SessionSource(platform=Platform.FEISHU, chat_id=f"shutdown-{i}", user_id="owner")
        evt = MessageEvent(text="work", message_id=f"shutdown-in-{i}", source=src)
        key = adapter._event_session_key(evt)
        turn = adapter.bridge.admit(evt, key)
        assert await adapter.bridge.ensure_card(turn)
        session = adapter.cards._sessions[evt.message_id]
        if i == 1:
            session.state = SessionState.CLARIFY_PAUSED
        turns.append(turn)
        ids.append(session.card_id)
    attempted = []

    async def close(ident, **_kwargs):
        attempted.append(ident)
        if ident == ids[0]:
            await asyncio.Event().wait()
        if ident == ids[1]:
            raise RuntimeError("injected single-card close failure")

    client.cardkit_close_streaming.side_effect = close

    # A fourth task is still creating its placeholder.
    async def creating(*_a, **_kw):
        await asyncio.Event().wait()

    client.cardkit_create.side_effect = creating
    src = SessionSource(platform=Platform.FEISHU, chat_id="creating", user_id="owner")
    evt = MessageEvent(text="work", message_id="shutdown-creating", source=src)
    turn = adapter.bridge.admit(evt, adapter._event_session_key(evt))
    adapter.cards.on_message_started(message_id=turn.message_id, chat_id=turn.chat_id, session_key=turn.session_key)
    creation = adapter.cards._sessions[turn.message_id].create_task
    await asyncio.sleep(0)
    from hermes_lark_streaming.native_reviews import NativeReviews
    from hermes_lark_streaming.review_receipts import ReviewReceiptBuffer

    assert adapter.reviews.buffer.capture(
        profile_home=adapter.cards._profile_home,
        chat_id="shutdown-receipt",
        session_key="receipt-key",
        platform="feishu",
        origin="background_review",
        payload={
            "tool_name": "memory",
            "args": {"action": "add", "target": "memory", "content": "audit"},
            "result": {"success": True, "message": "Memory updated"},
            "conversation_id": "receipt-session",
            "turn_id": "receipt-turn",
            "tool_call_id": "receipt-tool",
        },
    )
    with patch.object(adapter.cards, "_deliver_agent_status", new=AsyncMock(return_value=False)) as delivery:
        assert not await adapter.reviews.deliver("shutdown-receipt")
        assert not await NativeReviews(adapter.cards).deliver("shutdown-receipt")
        first, second = [call.kwargs["event"] for call in delivery.await_args_list]
        assert first.delivery_uuid and first.delivery_uuid == second.delivery_uuid
    before = time.monotonic()
    with (
        patch("hermes_lark_streaming.native_adapter.SHUTDOWN_BUDGET_SEC", 0.05),
        patch("hermes_lark_streaming.native_runtime.record_adapter", side_effect=OSError("injected record failure")),
        patch.object(FeishuAdapter, "disconnect", new=AsyncMock()) as disconnected,
    ):
        await adapter.disconnect()
        disconnected.assert_awaited_once()
    assert time.monotonic() - before < 0.5
    assert set(attempted) >= set(ids) and creation.done()
    reopened = ReviewReceiptBuffer(adapter.cards._profile_home / "plugin-data/hermes-lark-streaming/reviews.sqlite3")
    assert len(reopened.snapshot(adapter.cards._profile_home, "shutdown-receipt").receipts) == 1
    assert adapter.history is adapter.bridge.finished
    assert not adapter.bridge.messages and not adapter.bridge.turns and not adapter.cards._sessions
    assert not (await adapter.send("chat", "late")).success
    assert not (await adapter.send_or_update_status("chat", "status-key", "late")).success
    checks = {
        "receipt_confirmation_uuid_survives_delivery_reconstruction_without_schema_change": True,
        "shutdown_keeps_unacknowledged_receipts_in_existing_journal": True,
        "ordinary_slash_command_stays_cardified": True,
        "official_picker_and_update_prompt_coexist_with_custom_clarify_and_approval": True,
        "completed_card_edit_is_idempotent_and_unknown_or_cross_chat_edit_fails_closed": True,
        "shutdown_bounds_hanging_card_cancels_creation_and_disconnects_builtin": True,
        "shutdown_single_card_failure_does_not_block_other_cards": True,
    }
    checks.update(await reconnect_checks(adapter))
    return checks


async def reconnect_checks(retired: Any) -> dict[str, bool]:
    from types import SimpleNamespace

    from gateway.config import PlatformConfig
    from plugins.platforms.feishu.adapter import FeishuAdapter

    from hermes_lark_streaming.card_mutations import MUTATION_SCOPE
    from hermes_lark_streaming.feishu import FeishuClient, FeishuClientConfig
    from hermes_lark_streaming.native_hooks import ACTIVE

    registry = retired.cards._card_mutations
    old_client = FeishuClient(FeishuClientConfig("offline", "offline"), mutations=registry)
    lock = registry.state("reconnect-card").lock
    epoch = registry.epoch
    latest = dict(retired.cards._agent_status_manager.registry._cards)
    fresh = type(retired)(PlatformConfig(enabled=True, extra={"app_id": "offline", "app_secret": "offline"}))
    assert fresh.cards is retired.cards and registry.epoch == epoch + 1 and not registry.stopping
    assert registry.state("reconnect-card").lock is lock
    assert fresh.cards._agent_status_manager.registry._cards == latest
    old_client._client.cardkit.v1.card.asettings = AsyncMock()
    try:
        await old_client.cardkit_close_streaming("reconnect-card")
    except RuntimeError as error:
        assert "retired transport" in str(error)
    else:
        raise AssertionError("Retired client wrote after native adapter recreation")
    old_client._client.cardkit.v1.card.asettings.assert_not_called()
    # Retired review continuations cannot use the controller's new client either.
    assert not await retired.reviews.deliver("shutdown-receipt")
    with (
        patch.object(FeishuAdapter, "connect", new=AsyncMock(return_value=True)),
        patch.object(fresh, "schedule_reviews") as schedule,
    ):
        assert await fresh.connect(is_reconnect=True)
        schedule.assert_any_call("shutdown-receipt")
    assert fresh in ACTIVE
    fresh.cards._client._client.cardkit.v1.card.asettings = AsyncMock(
        return_value=SimpleNamespace(success=lambda: True)
    )
    await fresh.cards._client.cardkit_close_streaming("reconnect-card")
    fresh.cards._client._client.cardkit.v1.card.asettings.assert_awaited_once()
    with patch.object(FeishuAdapter, "disconnect", new=AsyncMock()):
        await fresh.disconnect()
    epoch = registry.epoch
    token = MUTATION_SCOPE.set((id(registry), epoch))
    try:
        with (
            patch.object(FeishuAdapter, "connect", new=AsyncMock(return_value=True)),
            patch.object(fresh, "schedule_reviews"),
        ):
            assert await fresh.connect(is_reconnect=True)
        assert registry.epoch == epoch + 1
        assert MUTATION_SCOPE.get() == (id(registry), epoch), "Reconnect leaked its admission into an old caller"
    finally:
        MUTATION_SCOPE.reset(token)
    assert not fresh.stopping and not fresh.bridge.stopping
    assert fresh.bridge._mutation_epoch == fresh.reviews._mutation_epoch == registry.epoch
    assert registry.state("reconnect-card").lock is lock
    with patch.object(FeishuAdapter, "disconnect", new=AsyncMock()):
        await fresh.disconnect()
    return {
        "native_adapter_recreation_rearms_cached_controller_without_replacing_locks_or_latest_refs": True,
        "retired_clients_and_review_continuations_cannot_write_after_reconnect": True,
        "same_adapter_reconnect_recovers_pending_reviews_without_leaking_epoch_context": True,
    }
