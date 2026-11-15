"""Persist native review facts and acknowledge only confirmed delivery."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from .agent_status import AgentStatusEvent, conversation_key
from .native_context import native_home
from .native_history import DeliveryHistory
from .review_receipts import ReviewReceiptBatch, ReviewReceiptBuffer

_buffers: dict[Path, ReviewReceiptBuffer] = {}


def review_buffer(home: Path) -> ReviewReceiptBuffer:
    home = home.resolve()
    if home not in _buffers:
        _buffers[home] = ReviewReceiptBuffer(home / "plugin-data/hermes-lark-streaming/reviews.sqlite3")
    return _buffers[home]


class NativeReviews:
    def __init__(self, controller: Any) -> None:
        self.controller = controller
        self.home = controller._profile_home
        self._profile_home = self.home
        self.buffer = review_buffer(self.home)
        self.lock = asyncio.Lock()
        self._covered: dict[str, list[str]] = {}
        self._coverage = DeliveryHistory()

    def covers(self, chat_id: str, text: str) -> bool:
        self._coverage.prune()
        for key in list(self._covered):
            if key not in self._coverage.records:
                self._covered.pop(key, None)
        return text in self._covered.get(chat_id, [])

    @native_home
    async def deliver(self, chat_id: str) -> bool:
        from agent.background_review import summarize_background_review_actions  # type: ignore[import-not-found]
        from agent.i18n import t  # type: ignore[import-not-found]

        if not self.controller.agent_status_enabled_for(self.home):
            return False
        async with self.lock:
            batch = self.buffer.snapshot(self.home, chat_id)
            if not batch.receipts:
                return True
            raw = (self.controller._cfg._reload().get("display") or {}).get("memory_notifications", "on")
            mode = ("on" if raw else "off") if isinstance(raw, bool) else str(raw or "on").lower()
            lines = batch.summarize(summarize_background_review_actions, mode)
            if not lines:
                self.buffer.acknowledge(batch)
                return True
            event = AgentStatusEvent(
                conversation_key=conversation_key(chat_id),
                text=t("display.review.summary_callback", summary=" · ".join(lines)),
                created_at=time.time(),
                source="background_review",
                delivery_uuid=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    json.dumps(
                        [receipt.identity for receipt in batch.receipts],
                        ensure_ascii=False,
                    ),
                ).hex,
            )
            delivered = await self.controller._deliver_agent_status(
                event=event,
                chat_id=chat_id,
            )
            if delivered:
                self.buffer.acknowledge(batch)
                # The default gateway callback may still arrive for a live
                # generation. Remember exact host-generated messages per
                # review so it cannot overwrite a merged old+new display.
                groups: dict[tuple[str, str], list[Any]] = {}
                for receipt in batch.receipts:
                    groups.setdefault((receipt.conversation_id, receipt.turn_id), []).append(receipt)
                covered = self._covered.setdefault(chat_id, [])
                covered.append(event.text)
                for receipts in groups.values():
                    actions = ReviewReceiptBatch(tuple(receipts)).summarize(summarize_background_review_actions, mode)
                    if actions:
                        covered.append(t("display.review.summary_callback", summary=" · ".join(actions)))
                self._covered[chat_id] = list(dict.fromkeys(covered))[-64:]
                self._coverage.add(chat_id, chat_id, "review")
                self.covers(chat_id, "")
            return bool(delivered)
