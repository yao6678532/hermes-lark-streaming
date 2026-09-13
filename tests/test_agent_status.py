"""Latest-card ordering, loop ownership, and lifecycle cleanup tests."""

from __future__ import annotations

import asyncio

import pytest

from hermes_lark_streaming.agent_status import (
    AGENT_STATUS_RETENTION_SEC,
    AgentStatusManager,
    LatestCardRegistry,
)


def test_older_session_registering_late_cannot_reclaim_latest_card() -> None:
    registry = LatestCardRegistry()
    older_session = object()
    newer_session = object()

    assert registry.set_active(
        key="feishu:chat",
        chat_id="chat",
        message_id="message-b",
        card_id="card-b",
        created_at=20.0,
        active_session=newer_session,
        sequence=1,
    )
    assert not registry.set_active(
        key="feishu:chat",
        chat_id="chat",
        message_id="message-a",
        card_id="card-a",
        created_at=10.0,
        active_session=older_session,
        sequence=1,
    )

    latest = registry.get("feishu:chat")
    assert latest is not None
    assert latest.card_id == "card-b"
    assert latest.active_session is newer_session


def test_same_session_physical_rollover_replaces_latest_card() -> None:
    registry = LatestCardRegistry()
    session = object()

    assert registry.set_active(
        key="feishu:chat",
        chat_id="chat",
        message_id="message-a1",
        card_id="card-a1",
        created_at=10.0,
        active_session=session,
        sequence=9,
    )
    assert registry.set_active(
        key="feishu:chat",
        chat_id="chat",
        message_id="message-a2",
        card_id="card-a2",
        created_at=10.0,
        active_session=session,
        sequence=1,
        agent_status="status X",
    )

    latest = registry.get("feishu:chat")
    assert latest is not None
    assert latest.card_id == "card-a2"
    assert latest.message_id == "message-a2"
    assert latest.agent_status == "status X"


def test_conversation_lock_is_replaced_after_event_loop_closes() -> None:
    manager = AgentStatusManager()
    loop_a = asyncio.new_event_loop()
    loop_b = asyncio.new_event_loop()
    try:
        lock_a = loop_a.run_until_complete(_get_lock(manager, "feishu:chat"))
        loop_a.close()
        lock_b = loop_b.run_until_complete(_get_lock(manager, "feishu:chat"))
    finally:
        if not loop_a.is_closed():
            loop_a.close()
        loop_b.close()

    assert lock_b is not lock_a


async def _get_lock(manager: AgentStatusManager, key: str) -> asyncio.Lock:
    lock = manager.lock_for(key)
    async with lock:
        return lock


@pytest.mark.asyncio
async def test_prune_removes_stale_finalized_ref_and_lock_but_retains_active() -> None:
    manager = AgentStatusManager()
    stale_session = object()
    active_session = object()
    now = 100_000.0
    stale_key = "feishu:stale"
    active_key = "feishu:active"

    assert manager.registry.set_active(
        key=stale_key,
        chat_id="stale",
        message_id="stale-message",
        card_id="stale-card",
        created_at=1.0,
        active_session=stale_session,
        sequence=3,
    )
    stale_ref = manager.registry.mark_finalized(
        key=stale_key,
        active_session=stale_session,
        card_snapshot={"body": {"elements": []}},
        sequence=4,
        agent_status=None,
    )
    assert stale_ref is not None
    stale_ref.updated_at = now - AGENT_STATUS_RETENTION_SEC - 1

    assert manager.registry.set_active(
        key=active_key,
        chat_id="active",
        message_id="active-message",
        card_id="active-card",
        created_at=1.0,
        active_session=active_session,
        sequence=1,
    )
    manager.lock_for(stale_key)
    manager.lock_for(active_key)

    assert manager.prune_stale(now=now) == {stale_key}
    assert manager.registry.get(stale_key) is None
    assert not manager.has_lock(stale_key)
    assert manager.registry.get(active_key) is not None
    assert manager.has_lock(active_key)
