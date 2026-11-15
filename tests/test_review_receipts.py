"""Receipt ownership, acknowledgment races and truthful filtering."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_lark_streaming.review_receipts import ReviewReceiptBuffer


def capture(buffer: ReviewReceiptBuffer, home: Path, **changes) -> bool:
    payload = {
        "tool_name": "memory", "args": {"action": "add", "target": "memory"},
        "result": {"success": True, "message": "Entry added.", "target": "memory"},
        "status": "ok", "conversation_id": "conversation", "turn_id": "review-a", "tool_call_id": "call-1",
    }
    payload.update(changes.pop("payload", {}))
    kwargs = dict(profile_home=home, chat_id="chat", session_key="session", platform="feishu",
                  origin="background_review", payload=payload)
    kwargs.update(changes)
    return buffer.capture(**kwargs)


def test_older_ack_cannot_erase_next_review_receipt(tmp_path):
    buffer = ReviewReceiptBuffer()
    assert capture(buffer, tmp_path)
    older = buffer.snapshot(tmp_path, "chat")
    assert capture(buffer, tmp_path, payload={"turn_id": "review-b"})
    buffer.acknowledge(older)
    assert [r.turn_id for r in buffer.snapshot(tmp_path, "chat").receipts] == ["review-b"]


def test_failed_delivery_keeps_facts_for_next_card(tmp_path):
    buffer = ReviewReceiptBuffer()
    assert capture(buffer, tmp_path)
    failed_batch = buffer.snapshot(tmp_path, "chat")
    assert buffer.snapshot(tmp_path, "chat") == failed_batch
    assert capture(buffer, tmp_path, payload={"turn_id": "review-b"})
    combined = buffer.snapshot(tmp_path, "chat")
    assert len(combined.receipts) == 2
    buffer.acknowledge(combined)
    assert not buffer.snapshot(tmp_path, "chat").receipts


def test_duplicate_receipt_is_idempotent_but_reused_call_id_in_new_turn_is_valid(tmp_path):
    buffer = ReviewReceiptBuffer()
    assert capture(buffer, tmp_path)
    assert not capture(buffer, tmp_path)
    assert capture(buffer, tmp_path, payload={"turn_id": "review-b"})
    assert len(buffer.snapshot(tmp_path, "chat").receipts) == 2


def test_same_chat_and_call_ids_do_not_cross_profiles(tmp_path):
    buffer = ReviewReceiptBuffer()
    assert capture(buffer, tmp_path / "profile-a")
    assert capture(buffer, tmp_path / "profile-b")
    a = buffer.snapshot(tmp_path / "profile-a", "chat")
    b = buffer.snapshot(tmp_path / "profile-b", "chat")
    assert len(a.receipts) == len(b.receipts) == 1
    buffer.acknowledge(a)
    assert buffer.snapshot(tmp_path / "profile-b", "chat") == b
    assert not buffer.snapshot(tmp_path / "profile-b", "another-chat").receipts


@pytest.mark.parametrize("changes", [
    {"origin": "assistant_tool"}, {"origin": "foreground"}, {"platform": "telegram"},
    {"chat_id": ""}, {"session_key": ""},
    {"payload": {"tool_name": "terminal"}}, {"payload": {"status": "cancelled"}},
    {"payload": {"status": "error"}}, {"payload": {"result": {"success": False}}},
    {"payload": {"result": "not JSON"}}, {"payload": {"result": []}},
    {"payload": {"tool_call_id": ""}}, {"payload": {"turn_id": ""}},
    {"payload": {"conversation_id": ""}},
])
def test_unowned_or_unconfirmed_events_never_become_update_receipts(tmp_path, changes):
    buffer = ReviewReceiptBuffer()
    assert not capture(buffer, tmp_path, **changes)
    assert not buffer.snapshot(tmp_path, "chat").receipts


def test_receipt_copies_mutable_hook_payload(tmp_path):
    buffer = ReviewReceiptBuffer()
    result = {"success": True, "target": "memory", "message": "Entry added."}
    args = {"action": "add", "target": "memory", "content": "fact"}
    assert capture(buffer, tmp_path, payload={"args": args, "result": result})
    args["target"] = "user"
    result["success"] = False
    receipt = buffer.snapshot(tmp_path, "chat").receipts[0]
    assert '"target": "memory"' in receipt.args_json
    assert '"success": true' in receipt.result_json


def test_journal_recovers_pending_facts_and_rejects_replay_after_ack(tmp_path):
    journal = tmp_path / "state/reviews.sqlite3"
    first = ReviewReceiptBuffer(journal)
    assert capture(first, tmp_path)
    restarted = ReviewReceiptBuffer(journal)
    batch = restarted.snapshot(tmp_path, "chat")
    assert len(batch.receipts) == 1
    assert capture(first, tmp_path, payload={"turn_id": "review-b"})
    restarted.acknowledge(batch)
    after_ack = ReviewReceiptBuffer(journal)
    assert [r.turn_id for r in after_ack.snapshot(tmp_path, "chat").receipts] == ["review-b"]
    assert not capture(after_ack, tmp_path)
    assert journal.stat().st_mode & 0o777 == 0o600


def test_journal_instances_share_atomic_deduplication(tmp_path):
    path = tmp_path / "reviews.sqlite3"
    first, second = ReviewReceiptBuffer(path), ReviewReceiptBuffer(path)
    assert capture(first, tmp_path)
    assert not capture(second, tmp_path)
    assert len(second.snapshot(tmp_path, "chat").receipts) == 1


def test_provider_call_id_reused_in_another_request_is_a_distinct_receipt(tmp_path):
    buffer = ReviewReceiptBuffer()
    assert capture(buffer, tmp_path, payload={"api_request_id": "request-a"})
    assert capture(buffer, tmp_path, payload={"api_request_id": "request-b"})
    assert not capture(buffer, tmp_path, payload={"api_request_id": "request-b"})
    assert len(buffer.snapshot(tmp_path, "chat").receipts) == 2
