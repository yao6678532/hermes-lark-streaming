"""Bounded transport ownership records; never retain a completed turn's content."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

LIMIT = 2048
TTL_SECONDS = 86400.0


@dataclass(frozen=True, slots=True)
class DeliveryIdentity:
    chat_id: str
    kind: str
    at: float


class DeliveryHistory:
    def __init__(self) -> None:
        self.records: OrderedDict[str, DeliveryIdentity] = OrderedDict()

    def prune(self) -> None:
        now = time.monotonic()
        while self.records:
            _ident, record = next(iter(self.records.items()))
            if len(self.records) <= LIMIT and now - record.at < TTL_SECONDS:
                break
            self.records.popitem(last=False)

    def add(self, ident: str | None, chat_id: str, kind: str) -> None:
        if ident:
            self.records.pop(ident, None)
            self.records[ident] = DeliveryIdentity(chat_id, kind, time.monotonic())
            self.prune()

    def get(self, ident: str) -> DeliveryIdentity | None:
        self.prune()
        return self.records.get(ident)
