"""Protocol failures against an independent in-memory CardKit SDK boundary."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_lark_streaming.card_documents import partition_card, serialized_size
from hermes_lark_streaming.card_mutations import CardMutations
from hermes_lark_streaming.feishu import FeishuAPIError, FeishuClient
from hermes_lark_streaming.native_history import DeliveryHistory


def response(code=0, **data):
    return SimpleNamespace(success=lambda: code == 0, code=code, msg="injected", data=SimpleNamespace(**data))


def lookup(value, ident):
    if isinstance(value, dict):
        if value.get("element_id") == ident:
            return value
        for child in value.values():
            if (found := lookup(child, ident)) is not None:
                return found
    if isinstance(value, list):
        for child in value:
            if (found := lookup(child, ident)) is not None:
                return found
    return None


class Server:
    """Model the server independently of the client's document application."""

    def __init__(self):
        self.cards, self.messages, self.sequences, self.uuids = {}, {}, {}, {}
        self.requests = []
        self.lost_insert_ack = False
        self.lost_message_ack = False
        self.reject_duplicate = False
        self.conflicts = 0
        self.stream_timeouts = 0

    def accept(self, operation, request):
        body = request.request_body
        card_id = request.card_id
        self.requests.append((operation, card_id, body.sequence, body.uuid))
        if self.conflicts:
            self.conflicts -= 1
            return response(300317)
        if body.sequence <= self.sequences.get(card_id, 0):
            return response(300317)
        self.sequences[card_id] = body.sequence
        return None

    async def create(self, request):
        card = json.loads(request.request_body.data)
        assert serialized_size(card) <= 27000
        ident = f"card-{len(self.cards)}"
        self.cards[ident] = card
        self.sequences[ident] = 0
        return response(card_id=ident)

    async def send(self, request):
        body = request.request_body
        if body.uuid in self.uuids:
            return response(message_id=self.uuids[body.uuid])
        card_id = json.loads(body.content)["data"]["card_id"]
        ident = f"message-{len(self.uuids)}"
        self.messages[ident] = card_id
        self.uuids[body.uuid] = ident
        if self.lost_message_ack and len(self.messages) > 1:
            self.lost_message_ack = False
            raise TimeoutError("message accepted; reply lost")
        return response(message_id=ident)

    async def update(self, request):
        if failure := self.accept("update", request):
            return failure
        card = json.loads(request.request_body.card.data)
        assert serialized_size(card) <= 27000
        self.cards[request.card_id] = card
        return response()

    async def settings(self, request):
        if failure := self.accept("settings", request):
            return failure
        settings = json.loads(request.request_body.settings)
        assert set(settings) == {"config"}
        self.cards[request.card_id].setdefault("config", {}).update(settings["config"])
        return response()

    def content(self, request):
        if failure := self.accept("content", request):
            return failure
        if self.stream_timeouts:
            self.stream_timeouts -= 1
            return response(200850)
        card = deepcopy(self.cards[request.card_id])
        assert card.get("config", {}).get("streaming_mode")
        lookup(card, request.element_id)["content"] = request.request_body.content
        assert serialized_size(card) <= 27000
        self.cards[request.card_id] = card
        return response()

    async def batch(self, request):
        if failure := self.accept("batch", request):
            return failure
        if self.reject_duplicate:
            return response(300301)
        card = self.cards[request.card_id]
        for action in json.loads(request.request_body.actions):
            params = action["params"]
            if action["action"] == "add_elements":
                if any(lookup(card, e["element_id"]) for e in params["elements"]):
                    return response(300301)
                root = card["body"]["elements"]
                index = next(i for i, e in enumerate(root) if e["element_id"] == params["target_element_id"])
                root[index:index] = deepcopy(params["elements"])
            elif action["action"] == "partial_update_element":
                lookup(card, params["element_id"]).update(params["partial_element"])
            else:
                raise AssertionError("Unexpected operation in fake server")
        if self.lost_insert_ack:
            self.lost_insert_ack = False
            raise TimeoutError("insert accepted; response lost")
        assert serialized_size(card) <= 27000
        return response()

    async def delete(self, request):
        self.messages.pop(request.message_id, None)
        return response()


def client_for(server):
    client = FeishuClient.__new__(FeishuClient)
    client._client = SimpleNamespace(
        cardkit=SimpleNamespace(
            v1=SimpleNamespace(
                card=SimpleNamespace(
                    acreate=server.create, aupdate=server.update, abatch_update=server.batch, asettings=server.settings
                ),
                card_element=SimpleNamespace(content=server.content),
            )
        ),
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message=SimpleNamespace(
                    areply=server.send,
                    acreate=server.send,
                    adelete=server.delete,
                )
            )
        ),
    )
    return client


def card(text="hello", streaming=True):
    return {
        "schema": "2.0",
        "config": {"streaming_mode": streaming, "width_mode": "default"},
        "body": {
            "elements": [
                {"tag": "markdown", "element_id": "answer", "content": text},
                {"tag": "markdown", "element_id": "loading_icon", "content": "loading"},
            ]
        },
    }


@pytest.mark.asyncio
async def test_sequence_conflicts_use_one_logical_uuid_and_share_physical_counter():
    server = Server()
    client = client_for(server)
    ident = await client.cardkit_create(card())
    server.conflicts = 3
    await client.cardkit_stream_element(ident, "answer", "answer", sequence=5)
    attempts = [r for r in server.requests if r[0] == "content"]
    assert [r[2] for r in attempts] == [5, 6, 7, 8]
    assert len({r[3] for r in attempts}) == 1
    await client.cardkit_close_streaming(ident, sequence=2)
    await client.cardkit_update(ident, card("finished", False), sequence=3)
    assert [r[2] for r in server.requests][-2:] == [9, 10]


@pytest.mark.asyncio
async def test_retry_rechecks_owner_and_limits_conflict_attempts():
    registry = CardMutations()
    state = registry.state("card")
    state.streaming = True
    calls = []

    async def call(seq, uid):
        calls.append(seq)
        state.owner = lambda _live: False
        raise FeishuAPIError("conflict", 300317)

    with pytest.raises(RuntimeError, match="owner changed"):
        await registry.run("card", sequence=1, operation_id=None, call=call, reopen=call, stream=True)
    assert len(calls) == 1
    state.owner = None

    async def conflicts(seq, uid):
        calls.append(seq)
        raise FeishuAPIError("conflict", 300317)

    with pytest.raises(FeishuAPIError):
        await registry.run("card", sequence=1, operation_id=None, call=conflicts, reopen=conflicts, stream=True)
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_renew_after_ten_minutes_and_one_timeout_recovery_then_never_reopen_closed_card():
    server = Server()
    client = client_for(server)
    ident = await client.cardkit_create(card())
    state = client.mutations.state(ident)
    state.opened_at = 0
    server.stream_timeouts = 1
    clock = SimpleNamespace(monotonic=lambda: 700.0)
    with patch("hermes_lark_streaming.card_mutations.time", clock):
        await client.cardkit_stream_element(ident, "answer", "long task")
    assert [r[0] for r in server.requests] == ["settings", "content", "settings", "content"]
    await client.cardkit_close_streaming(ident)
    count = len(server.requests)
    with pytest.raises(RuntimeError, match="already closed"):
        await client.cardkit_stream_element(ident, "answer", "late")
    assert len(server.requests) == count


@pytest.mark.asyncio
async def test_duplicate_id_only_recovers_the_exact_insert_with_unknown_previous_result():
    server = Server()
    client = client_for(server)
    ident = await client.cardkit_create(card())
    actions = [
        {
            "action": "add_elements",
            "params": {
                "type": "insert_before",
                "target_element_id": "loading_icon",
                "elements": [{"tag": "markdown", "element_id": "new", "content": "tool"}],
            },
        }
    ]
    server.lost_insert_ack = True
    with pytest.raises(TimeoutError):
        await client.cardkit_batch_update(ident, actions)
    await client.cardkit_batch_update(ident, actions)
    assert [r[0] for r in server.requests] == ["batch", "batch", "update"]
    assert server.requests[0][3] == server.requests[1][3]
    assert sum(e["element_id"] == "new" for e in server.cards[ident]["body"]["elements"]) == 1
    server.reject_duplicate = True
    with pytest.raises(FeishuAPIError):
        await client.cardkit_batch_update(
            ident,
            [
                {
                    "action": "add_elements",
                    "params": {
                        "type": "insert_before",
                        "target_element_id": "loading_icon",
                        "elements": [{"tag": "markdown", "element_id": "other", "content": "x"}],
                    },
                }
            ],
        )
    assert server.requests[-1][0] == "batch"


@pytest.mark.asyncio
async def test_byte_rollover_preserves_unicode_final_footer_and_lost_send_ack_without_duplicates():
    server = Server()
    client = client_for(server)
    ident = await client.cardkit_create(card())
    await client.reply_card_by_id("anchor", ident)
    text = "段落😀\n\n" * 11000
    server.lost_message_ack = True
    await client.cardkit_stream_element(ident, "answer", text)
    doc = client.mutations.documents[ident]
    assert len(doc.parts) > 2
    assert len(server.messages) == len(doc.parts)
    assert "".join(lookup(server.cards[p], "answer")["content"] for p in doc.parts) == text
    final = card(text, False)
    final["body"]["elements"] = [
        final["body"]["elements"][0],
        {"tag": "hr", "element_id": "run_details_divider"},
        {
            "tag": "collapsible_panel",
            "element_id": "details",
            "elements": [{"tag": "markdown", "content": "Tokens: 123 · Context · Quota"}],
        },
    ]
    await client.cardkit_close_streaming(ident)
    await client.cardkit_update(ident, final)
    assert all(serialized_size(server.cards[p]) <= 27000 for p in doc.parts)
    assert all(lookup(server.cards[p], "details") is None for p in doc.parts[:-1])
    assert lookup(server.cards[doc.parts[-1]], "details") is not None
    await client.cardkit_update(ident, card("corrected short answer", False))
    assert len(doc.parts) == len(server.messages) == 1
    assert lookup(server.cards[ident], "answer")["content"] == "corrected short answer"


def test_small_card_layout_unchanged_and_nested_tool_output_is_not_truncated():
    original = card("short")
    assert partition_card(original) == [original]
    huge = card()
    huge["body"]["elements"][0] = {
        "tag": "collapsible_panel",
        "element_id": "tools",
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"step-{i}: " + "汉😀" * 1600}} for i in range(25)
        ],
    }
    parts = partition_card(huge)
    originals = [e["text"]["content"] for e in huge["body"]["elements"][0]["elements"]]
    outputs = []
    for part in parts:
        panel = lookup(part, "tools")
        if panel:
            outputs.extend(e["text"]["content"] for e in panel["elements"])
    assert outputs == originals
    assert all(serialized_size(p) <= 27000 for p in parts)


def test_completed_history_limit_ttl_and_profile_ownership():
    history = DeliveryHistory()
    for i in range(3000):
        history.add(str(i), "chat", "card")
    assert len(history.records) == 2048
    assert history.get("0") is None
    now = history.get("2999").at
    with patch("hermes_lark_streaming.native_history.time", SimpleNamespace(monotonic=lambda: now + 86401)):
        assert history.get("2999") is None
    assert DeliveryHistory().get("2999") is None


@pytest.mark.asyncio
async def test_initial_delivery_unknown_keeps_entity_and_confirms_same_uuid_after_final_write(monkeypatch):
    server = Server()
    client = client_for(server)
    ident = await client.cardkit_create(card())
    attempts = []

    async def unacknowledged(request):
        attempts.append(request.request_body.uuid)
        await server.send(request)  # All four sends refer to the same accepted message.
        raise TimeoutError("accepted but no acknowledgement")

    client._client.im.v1.message.areply = unacknowledged
    monkeypatch.setattr("hermes_lark_streaming.feishu._TRANSIENT_RETRY_DELAYS_SEC", (0, 0, 0))
    handle = await client.reply_card_by_id("anchor", ident)
    doc = client.mutations.documents[ident]
    assert handle.startswith("hls-pending:") and doc.unconfirmed and doc.messages == [None]
    assert len(server.messages) == 1 and len(set(attempts)) == 1
    client._client.im.v1.message.areply = server.send
    await client.cardkit_close_streaming(ident)
    await client.cardkit_update(ident, card("Final authoritative body", False))
    assert not doc.unconfirmed and doc.messages == ["message-0"]
    assert len(server.messages) == 1 and lookup(server.cards[ident], "answer")["content"] == "Final authoritative body"


def test_code_block_images_and_atomic_footer_survive_byte_projection():
    text = "```python\n" + "print('中文😀')\n" * 6000 + "```\n"
    original = card(text, False)
    original["body"]["elements"] += [
        {"tag": "img", "element_id": "picture", "img_key": "img-key", "alt": {"tag": "plain_text", "content": "图"}},
        {"tag": "hr", "element_id": "run_details_divider"},
        {"tag": "markdown", "element_id": "details", "content": "Tokens Context Quota" * 130},
    ]
    parts = partition_card(original)
    payload = []
    for part in parts:
        assert serialized_size(part) <= 27000
        node = lookup(part, "answer")
        if node:
            chunk = node["content"]
            assert chunk.startswith("```python\n") and chunk.rstrip().endswith("```")
            payload.append(chunk[len("```python\n") : chunk.rfind("```")].strip("\n"))
    assert "\n".join(payload) + "\n" == "print('中文😀')\n" * 6000
    assert sum(lookup(p, "picture") is not None for p in parts) == 1
    assert lookup(parts[-1], "details") and lookup(parts[-1], "run_details_divider")
    assert not any(lookup(p, "details") or lookup(p, "run_details_divider") for p in parts[:-1])


@pytest.mark.asyncio
async def test_same_card_serializes_foreground_and_late_footer_writes():
    server = Server()
    primary = client_for(server)
    late = client_for(server)
    late._mutations = primary.mutations
    ident = await primary.cardkit_create(card())
    await asyncio.gather(
        primary.cardkit_stream_element(ident, "answer", "body", sequence=1),
        late.cardkit_batch_update(
            ident,
            [
                {
                    "action": "partial_update_element",
                    "params": {"element_id": "loading_icon", "partial_element": {"content": "Memory updated"}},
                }
            ],
            sequence=1,
        ),
    )
    assert [r[2] for r in server.requests] == [1, 2]
    assert lookup(server.cards[ident], "answer")["content"] == "body"
    assert lookup(server.cards[ident], "loading_icon")["content"] == "Memory updated"


@pytest.mark.asyncio
async def test_sdk_serialization_and_repeated_renewals_use_protocol_shape():
    from lark_oapi.core.json import JSON

    server = Server()
    client = client_for(server)
    ident = await client.cardkit_create(card())
    captured = []
    original = server.settings

    async def settings(request):
        wire = json.loads(JSON.marshal(request.request_body))
        assert wire["uuid"] and isinstance(wire["sequence"], int)
        assert json.loads(wire["settings"]) == {"config": {"streaming_mode": True}}
        captured.append(wire)
        return await original(request)

    client._client.cardkit.v1.card.asettings = settings
    state = client.mutations.state(ident)
    state.opened_at = 0
    for now in (539, 540, 1079, 1080, 1620):
        with patch("hermes_lark_streaming.card_mutations.time", SimpleNamespace(monotonic=lambda now=now: float(now))):
            await client.cardkit_stream_element(ident, "answer", str(now))
    assert len(captured) == 3 and len({body["uuid"] for body in captured}) == 3


@pytest.mark.asyncio
async def test_unknown_final_update_result_never_claims_confirmed_delivery(monkeypatch):
    from hermes_lark_streaming.feishu import DeliveryUncertain

    server = Server()
    client = client_for(server)
    ident = await client.cardkit_create(card())
    await client.reply_card_by_id("anchor", ident)
    await client.cardkit_close_streaming(ident)
    attempts = []

    async def update(request):
        attempts.append(request.request_body.uuid)
        # Network outcome remains unknown, even though the final entity was updated.
        server.cards[ident] = json.loads(request.request_body.card.data)
        raise TimeoutError("accepted; no response")

    client._client.cardkit.v1.card.aupdate = update
    monkeypatch.setattr("hermes_lark_streaming.feishu._TRANSIENT_RETRY_DELAYS_SEC", (0, 0, 0))
    with pytest.raises(DeliveryUncertain):
        await client.cardkit_update(ident, card("final", False))
    assert client.mutations.documents[ident].write_uncertain
    assert len(attempts) == 4 and len(set(attempts)) == 1 and len(server.messages) == 1


@pytest.mark.asyncio
async def test_shutdown_deadline_prevents_new_requests_even_from_cleanup_context():
    from hermes_lark_streaming.card_mutations import SHUTDOWN_WRITES

    registry = CardMutations()
    registry.stopping = True
    registry.shutdown_deadline = 0
    token = SHUTDOWN_WRITES.set(True)
    try:
        with pytest.raises(RuntimeError, match="shutdown"):
            registry.check_writes()
    finally:
        SHUTDOWN_WRITES.reset(token)


@pytest.mark.asyncio
async def test_registry_pressure_never_discards_live_card_or_lock_waiters():
    registry = CardMutations()
    live = registry.state("live")
    live.streaming = True
    lock = registry.state("waiting").lock
    await lock.acquire()
    waiter = asyncio.create_task(lock.acquire())
    await asyncio.sleep(0)
    lock.release()  # Unlocked, but the scheduled waiter still owns its position.
    for i in range(2200):
        registry.state(str(i))
    assert registry.state("live") is live and registry.state("waiting").lock is lock
    await waiter
    lock.release()


@pytest.mark.asyncio
async def test_unknown_receipt_text_retry_reuses_caller_identity(monkeypatch):
    from hermes_lark_streaming.feishu import DeliveryUncertain

    server = Server()
    client = client_for(server)
    calls = []

    async def send(request):
        calls.append(request.request_body.uuid)
        raise TimeoutError("unknown")

    client._client.im.v1.message.acreate = send
    monkeypatch.setattr("hermes_lark_streaming.feishu._TRANSIENT_RETRY_DELAYS_SEC", (0, 0, 0))
    for _ in range(2):
        with pytest.raises(DeliveryUncertain):
            await client.send_text_to_chat("chat", "Memory updated", operation_id="receipt-batch-identity")
    assert calls == ["receipt-batch-identity"] * 8


def test_startup_contract_detects_removed_required_parameters():
    from hermes_lark_streaming.native_contract import adapter_contract_errors

    class Broken:
        def send(self, chat_id, content, reply_to=None):
            pass

    errors = adapter_contract_errors(Broken)
    assert any("FeishuAdapter.send:" in error for error in errors)
    assert any("on_processing_complete missing" in error for error in errors)


@pytest.mark.asyncio
async def test_rearmed_transport_fences_old_client_and_old_context_without_replacing_locks():
    from hermes_lark_streaming.card_mutations import MUTATION_SCOPE

    server = Server()
    old = client_for(server)
    ident = await old.cardkit_create(card())
    await old.reply_card_by_id("anchor", ident)
    registry = old.mutations
    lock, doc = registry.state(ident).lock, registry.documents[ident]
    old_epoch = registry.epoch
    registry.stopping = True
    registry.rearm()
    fresh = client_for(server)
    fresh._mutations = registry
    with pytest.raises(RuntimeError, match="retired transport"):
        await old.cardkit_update(ident, card("old", False))
    token = MUTATION_SCOPE.set((id(registry), old_epoch))
    try:
        with pytest.raises(RuntimeError, match="retired transport"):
            await fresh.cardkit_update(ident, card("old continuation", False))
    finally:
        MUTATION_SCOPE.reset(token)
    await fresh.cardkit_update(ident, card("new transport", False))
    assert registry.state(ident).lock is lock and registry.documents[ident] is doc
    assert lookup(server.cards[ident], "answer")["content"] == "new transport"


@pytest.mark.asyncio
async def test_rearm_rejects_result_from_an_operation_that_started_before_disconnect():
    registry = CardMutations()
    started, release = asyncio.Event(), asyncio.Event()

    async def call(seq, uid):
        started.set()
        await release.wait()

    task = asyncio.create_task(registry.run("card", sequence=1, operation_id=None, call=call, reopen=call))
    await started.wait()
    lock = registry.state("card").lock
    registry.stopping = True
    registry.rearm()
    release.set()
    with pytest.raises(RuntimeError, match="retired transport"):
        await task
    assert registry.state("card").lock is lock


@pytest.mark.asyncio
async def test_independent_delivery_client_does_not_inherit_another_transports_shutdown():
    from hermes_lark_streaming.card_mutations import MUTATION_SCOPE

    retired = CardMutations()
    retired.stopping = True
    token = MUTATION_SCOPE.set((id(retired), retired.epoch))
    try:
        server = Server()
        independent = client_for(server)
        ident = await independent.cardkit_create(card("Independent Cron delivery", False))
        await independent.send_card_id_to_chat("cron-chat", ident)
        assert len(server.messages) == 1
    finally:
        MUTATION_SCOPE.reset(token)
