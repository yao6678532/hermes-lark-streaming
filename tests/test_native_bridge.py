"""Turn identity and terminal reconciliation contracts for the native bridge."""

from __future__ import annotations

import asyncio
import itertools
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hermes_lark_streaming.controller import StreamCardController
from hermes_lark_streaming.native_bridge import NativeBridge
from hermes_lark_streaming.streaming.segments import SegmentType


def event(message="inbound", anchor=None):
    return SimpleNamespace(
        message_id=message,
        reply_to_message_id=anchor,
        source=SimpleNamespace(chat_id="chat", user_id="owner", user_id_alt=None),
    )


def bridge_for(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "streaming: {enabled: true}\nfeishu: {app_id: app, app_secret: secret}\n"
        "display: {platforms: {feishu: {show_reasoning: true}}}\n"
    )
    ctrl = StreamCardController(tmp_path)
    ctrl._initialized = True
    ctrl._loop = asyncio.get_running_loop()
    ctrl._client = AsyncMock()
    ctrl._client.cardkit_create.return_value = "card"
    ctrl._client.reply_card_by_id.return_value = "card-message"
    return NativeBridge(ctrl)


@pytest.mark.asyncio
async def test_api_end_is_not_turn_end_and_final_snapshot_replaces_draft(tmp_path):
    bridge = bridge_for(tmp_path)
    turn = bridge.admit(event(), "session")
    await bridge.observe("start", "session", {"turn_id": "run", "api_call_count": 1})
    await bridge.frame(turn, "draft prefix", stream_id="segment-1", finalize=False)
    await bridge.observe(
        "usage",
        "session",
        {
            "turn_id": "run",
            "api_call_count": 1,
            "api_request_id": "api-1",
            "_answer": "Final corrected answer",
        },
    )
    await bridge.frame(turn, "Final corrected answer", stream_id="segment-1", finalize=True)
    assert not turn.completed
    session = bridge.controller._sessions[turn.message_id]
    assert [s.text for s in session.active_segments() if s.type == SegmentType.ANSWER] == ["Final corrected answer"]
    bridge.controller._client.cardkit_close_streaming.assert_not_awaited()
    assert await bridge.complete(turn)


@pytest.mark.asyncio
async def test_authoritative_reasoning_repairs_dropped_deltas_and_rejects_late_delta(tmp_path):
    bridge = bridge_for(tmp_path)
    turn = bridge.admit(event(), "session")
    await bridge.observe("start", "session", {"turn_id": "run", "api_call_count": 1})
    await bridge.observe(
        "reasoning",
        "session",
        {
            "turn_id": "run",
            "iteration": 1,
            "kind": "reasoning",
            "delta": "broken prefix",
        },
    )
    await bridge.observe(
        "usage",
        "session",
        {
            "turn_id": "run",
            "api_call_count": 1,
            "api_request_id": "api-1",
            "_reasoning": "Complete reasoning",
        },
    )
    await bridge.observe(
        "reasoning",
        "session",
        {
            "turn_id": "run",
            "iteration": 1,
            "kind": "reasoning",
            "delta": " late duplicate",
        },
    )
    session = bridge.controller._sessions[turn.message_id]
    assert [s.text for s in session.active_segments() if s.type == SegmentType.REASONING] == ["Complete reasoning"]
    assert await bridge.complete(turn)


def test_message_identity_is_separate_from_quoted_anchor_and_old_turn_cannot_rebind_new():
    bridge = NativeBridge(SimpleNamespace())
    first = bridge.admit(event("first", "quoted-message"), "session")
    first.turn_id = "run-first"
    second = bridge.admit(event("second", "quoted-message"), "session")
    second.turn_id = "run-second"
    assert second.message_id == "second" and second.anchor_id == "quoted-message"
    assert bridge.route("session", {"_inbound_id": "first", "turn_id": "run-first"}) is None
    assert bridge.route("session", {"turn_id": "run-first"}) is None
    assert bridge.route("session", {"turn_id": "run-second"}) is second


@pytest.mark.asyncio
async def test_approval_pause_does_not_trigger_clarify_split(tmp_path):
    bridge = bridge_for(tmp_path)
    turn = bridge.admit(event(), "session")
    await bridge.observe("start", "session", {"api_call_count": 1})
    for kind in ("human_request", "human_resolved"):
        await bridge.observe(kind, "session", {"kind": "approval", "request_id": "approval"})
    session = bridge.controller._sessions[turn.message_id]
    assert not session.clarify_pending_split
    assert not turn.pending_inputs
    assert not turn.completed
    assert await bridge.complete(turn)


@pytest.mark.asyncio
async def test_raw_provider_answer_is_never_rendered_without_host_transport(tmp_path):
    bridge = bridge_for(tmp_path)
    turn = bridge.admit(event(), "session")
    await bridge.observe("start", "session", {"api_call_count": 1})
    await bridge.observe(
        "usage",
        "session",
        {
            "api_call_count": 1,
            "api_request_id": "api",
            "_answer": "PRIVATE untransformed model draft",
        },
    )
    await bridge.frame(turn, "Host approved final", stream_id="frame", finalize=True)
    assert await bridge.complete(turn)
    card = bridge.controller._client.cardkit_update.await_args.args[1]
    assert "PRIVATE" not in json.dumps(card)
    assert "Host approved final" in json.dumps(card)


@pytest.mark.asyncio
async def test_native_reconciliation_keeps_every_answer_once_across_rollover(tmp_path):
    bridge = bridge_for(tmp_path)
    client = bridge.controller._client
    ids = itertools.count()
    client.cardkit_create.side_effect = lambda *_args: f"card-{next(ids)}"
    client.reply_card_by_id.side_effect = lambda *_args: f"message-{next(ids)}"
    turn = bridge.admit(event(), "session")
    with patch("hermes_lark_streaming.streaming.controller.ELEMENT_THRESHOLD", 20):
        for iteration in range(1, 9):
            await bridge.observe("start", "session", {"api_call_count": iteration})
            await bridge.frame(turn, f"Unique answer {iteration}.", stream_id=str(iteration), finalize=False)
            session = bridge.controller._sessions[turn.message_id]
            await bridge.controller._do_flush(session)
            await bridge.observe(
                "usage",
                "session",
                {
                    "api_call_count": iteration,
                    "api_request_id": f"request-{iteration}",
                    "_reasoning": f"Unique reasoning {iteration}.",
                    "_answer": f"Unique answer {iteration}.",
                },
            )
            await bridge.controller._do_flush(session)
        assert await bridge.complete(turn)
    assert client.cardkit_create.await_count > 1, "Test did not exercise real card rollover"
    cards = {call.args[0]: call.args[1] for call in client.cardkit_update.await_args_list}
    bodies = json.dumps([card["body"] for card in cards.values()])
    for iteration in range(1, 9):
        assert bodies.count(f"Unique answer {iteration}.") == 1, bodies
        assert bodies.count(f"Unique reasoning {iteration}.") == 1, bodies


@pytest.mark.asyncio
async def test_host_owned_silent_tools_and_interactions_survive_gc_then_release_heavy_indexes(tmp_path):
    bridge = bridge_for(tmp_path)
    ctrl = bridge.controller
    ctrl._native_owned = lambda s: s.message_id in bridge.messages
    turn = bridge.admit(event(), "session")
    await bridge.frame(turn, "body", stream_id="stream", finalize=False)
    session = ctrl._sessions[turn.message_id]
    session.created_at = session.last_activity_at = 0
    ctrl._prune_stale_sessions()
    assert ctrl._sessions[turn.message_id] is session
    for kind in ("clarify", "approval"):
        await bridge.observe("human_request", "session", {"kind": kind, "request_id": kind})
        ctrl._prune_stale_sessions()
        assert ctrl._sessions[turn.message_id] is session
        await bridge.observe("human_resolved", "session", {"kind": kind, "request_id": kind})
    await bridge.complete(turn)
    assert not bridge.messages and not bridge.turns and not ctrl._sessions and not ctrl._session_keys
    assert not turn.answer_segments and not turn.context and not turn.requests
    assert bridge.finished.get(turn.message_id) is not None


@pytest.mark.asyncio
async def test_orphan_gc_seals_before_detaching_latest_snapshot(tmp_path):
    bridge = bridge_for(tmp_path)
    turn = bridge.admit(event(), "session")
    await bridge.frame(turn, "orphan body", stream_id="stream", finalize=False)
    ctrl = bridge.controller
    session = ctrl._sessions[turn.message_id]
    session.created_at = session.last_activity_at = 0
    ctrl._native_owned = lambda _s: False
    ctrl._prune_stale_sessions()
    for _ in range(30):
        await asyncio.sleep(0)
        if not ctrl._sessions:
            break
    assert not ctrl._sessions
    from hermes_lark_streaming.agent_status import conversation_key

    ref = ctrl._agent_status_manager.registry.get(conversation_key("chat"))
    assert ref.finalized and "orphan body" in json.dumps(ref.card_snapshot)
