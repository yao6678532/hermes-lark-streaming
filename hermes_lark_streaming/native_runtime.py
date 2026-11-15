"""Profile-owned evidence of the adapter actually constructed by a gateway."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def record_adapter(home: Path, *, connected: bool, adapter: str) -> None:
    from utils import atomic_json_write  # type: ignore[import-not-found]

    path = home / "plugin-data/hermes-lark-streaming/runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json_write(path, {"pid": os.getpid(), "adapter": adapter, "connected": connected, "home": str(home)})


def running_adapter(home: Path) -> dict[str, Any] | None:
    path = home / "plugin-data/hermes-lark-streaming/runtime.json"
    try:
        record = json.loads(path.read_text())
        if not isinstance(record, dict) or record.get("home") != str(home.resolve()):
            return None
        os.kill(int(record["pid"]), 0)
        return record
    except (OSError, ValueError, TypeError, KeyError):
        return None
