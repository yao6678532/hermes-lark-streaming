"""Native background-review receipts; registration is owned by the plugin entry point.

Receipts are confirmed tool results, not inferred updates. The host's own
review summarizer determines their display text. Transport acknowledgment is
separate so a new receipt cannot be lost while an older batch is being shown.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ReviewReceipt:
    profile_home: Path
    chat_id: str
    session_key: str
    conversation_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    args_json: str
    result_json: str
    api_request_id: str = ""

    @property
    def identity(self) -> tuple[str, ...]:
        return (
            str(self.profile_home), self.chat_id, self.conversation_id,
            self.turn_id, self.api_request_id, self.tool_call_id,
        )


@dataclass(frozen=True)
class ReviewReceiptBatch:
    receipts: tuple[ReviewReceipt, ...]

    def summarize(self, summarizer: Callable[..., list[str]], mode: str = "on") -> list[str]:
        messages: list[dict[str, Any]] = []
        for index, receipt in enumerate(self.receipts):
            # Provider call ids may repeat across turns. Give each receipt a
            # local id while reconstructing this *new results only* transcript.
            call_id = f"receipt-{index}"
            messages.extend((
                {"role": "assistant", "tool_calls": [{
                    "id": call_id, "type": "function", "function": {
                        "name": receipt.tool_name, "arguments": receipt.args_json,
                    },
                }]},
                {"role": "tool", "tool_call_id": call_id, "content": receipt.result_json},
            ))
        return list(dict.fromkeys(summarizer(messages, [], notification_mode=mode)))


class ReviewReceiptBuffer:
    """Profile/chat-owned pending facts; never cleared by a new conversation turn."""

    def __init__(self, journal: Path | None = None) -> None:
        self._pending: dict[tuple[str, ...], ReviewReceipt] = {}
        self._seen: set[tuple[str, ...]] = set()
        self._lock = threading.Lock()
        self._journal = journal
        if journal is not None:
            journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self._database() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS receipts ("
                    "identity TEXT PRIMARY KEY, home TEXT NOT NULL, chat TEXT NOT NULL, "
                    "payload TEXT NOT NULL, acknowledged REAL)"
                )
                # Keep a bounded-duration replay fence, never evict pending facts.
                db.execute("DELETE FROM receipts WHERE acknowledged < ?", (time.time() - 14 * 86400,))
            journal.chmod(0o600)

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        assert self._journal is not None
        with closing(sqlite3.connect(self._journal, timeout=5)) as db, db:
            yield db

    def capture(
        self, *, profile_home: Path, chat_id: str, session_key: str, platform: str,
        origin: str, payload: dict[str, Any],
    ) -> bool:
        if origin != "background_review" or platform not in {"feishu", "lark"}:
            return False
        if not chat_id or not session_key:
            return False
        name = payload.get("tool_name")
        if name not in {"memory", "skill_manage"}:
            return False
        if payload.get("status") not in {None, "ok"}:
            return False
        result = payload.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (ValueError, TypeError):
                return False
        if not isinstance(result, dict) or result.get("success") is not True:
            return False
        if not all(payload.get(key) for key in ("conversation_id", "turn_id", "tool_call_id")):
            return False
        receipt = ReviewReceipt(
            profile_home=profile_home.resolve(), chat_id=chat_id, session_key=session_key,
            conversation_id=str(payload["conversation_id"]), turn_id=str(payload["turn_id"]),
            tool_call_id=str(payload["tool_call_id"]), tool_name=str(name),
            args_json=json.dumps(payload.get("args") or {}, ensure_ascii=False),
            result_json=json.dumps(result, ensure_ascii=False),
            api_request_id=str(payload.get("api_request_id") or ""),
        )
        with self._lock:
            if self._journal is not None:
                data = {**asdict(receipt), "profile_home": str(receipt.profile_home)}
                with self._database() as db:
                    changed = db.execute(
                        "INSERT OR IGNORE INTO receipts VALUES (?, ?, ?, ?, NULL)",
                        (json.dumps(receipt.identity), str(receipt.profile_home), chat_id,
                         json.dumps(data, ensure_ascii=False)),
                    ).rowcount
                return changed == 1
            if receipt.identity in self._seen:
                return False
            self._seen.add(receipt.identity)
            self._pending[receipt.identity] = receipt
        return True

    def snapshot(self, profile_home: Path, chat_id: str) -> ReviewReceiptBatch:
        home = profile_home.resolve()
        with self._lock:
            if self._journal is not None:
                with self._database() as db:
                    rows = db.execute(
                        "SELECT payload FROM receipts WHERE home = ? AND chat = ? "
                        "AND acknowledged IS NULL ORDER BY rowid", (str(home), chat_id),
                    ).fetchall()
                receipts = []
                for (raw,) in rows:
                    data = json.loads(raw)
                    data["profile_home"] = Path(data["profile_home"])
                    receipts.append(ReviewReceipt(**data))
                return ReviewReceiptBatch(tuple(receipts))
            return ReviewReceiptBatch(tuple(
                receipt for receipt in self._pending.values()
                if receipt.profile_home == home and receipt.chat_id == chat_id
            ))

    def acknowledge(self, batch: ReviewReceiptBatch) -> None:
        with self._lock:
            if self._journal is not None:
                with self._database() as db:
                    db.executemany(
                        "UPDATE receipts SET acknowledged = ? WHERE identity = ? AND acknowledged IS NULL",
                        ((time.time(), json.dumps(receipt.identity)) for receipt in batch.receipts),
                    )
                return
            for receipt in batch.receipts:
                if self._pending.get(receipt.identity) is receipt:
                    del self._pending[receipt.identity]

    def pending_chats(self, profile_home: Path) -> list[str]:
        home = profile_home.resolve()
        with self._lock:
            if self._journal is not None:
                with self._database() as db:
                    return [row[0] for row in db.execute(
                        "SELECT DISTINCT chat FROM receipts WHERE home = ? AND acknowledged IS NULL", (str(home),),
                    )]
            return sorted({receipt.chat_id for receipt in self._pending.values() if receipt.profile_home == home})


def observe_native_result(buffer: ReviewReceiptBuffer, **payload: Any) -> bool:
    """Official hook + read-only host provenance query; no core callback replacement."""
    from gateway.session_context import get_session_env  # type: ignore[import-not-found]
    from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
    from tools.skill_provenance import get_current_write_origin  # type: ignore[import-not-found]

    return buffer.capture(
        profile_home=Path(get_hermes_home()),
        chat_id=get_session_env("HERMES_SESSION_CHAT_ID") or "",
        session_key=get_session_env("HERMES_SESSION_KEY") or "",
        platform=get_session_env("HERMES_SESSION_PLATFORM") or "",
        origin=get_current_write_origin(), payload=payload,
    )
