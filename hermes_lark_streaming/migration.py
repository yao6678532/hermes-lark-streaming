"""Idempotent configuration additions required by the native host transport."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def prepare_config(original: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = deepcopy(original)
    changes: list[dict[str, Any]] = []
    streaming = config.get("streaming") or {}
    if not isinstance(streaming, dict) or not streaming.get("enabled"):
        return config, changes

    def assign(parent: dict[str, Any], key: str, value: Any, path: str) -> None:
        if parent.get(key) != value:
            changes.append({"path": path, "before": parent.get(key), "after": value})
            parent[key] = value

    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("plugins must be a mapping")
    enabled = plugins.get("enabled") or []
    disabled = plugins.get("disabled") or []
    if not isinstance(enabled, list) or not isinstance(disabled, list):
        raise ValueError("plugins.enabled and plugins.disabled must be lists")
    owned = {"hermes-lark-streaming", "platforms/feishu", "feishu-platform"}
    assign(
        plugins,
        "enabled",
        list(dict.fromkeys([*enabled, "platforms/feishu", "hermes-lark-streaming"])),
        "plugins.enabled",
    )
    if any(name in owned for name in disabled):
        assign(plugins, "disabled", [name for name in disabled if name not in owned], "plugins.disabled")
    display = config.get("display") or {}
    if not isinstance(display, dict):
        raise ValueError("display must be a mapping")
    platforms = display.get("platforms") or {}
    feishu = platforms.get("feishu") or {} if isinstance(platforms, dict) else {}
    if not isinstance(feishu, dict):
        raise ValueError("display.platforms.feishu must be a mapping")
    if feishu.get("show_reasoning", display.get("show_reasoning", False)):
        assign(plugins, "stream_reasoning_deltas", True, "plugins.stream_reasoning_deltas")
    # The old controller refreshed every 100ms independently of host edit
    # transport defaults. Existing explicit tuning remains authoritative.
    for key, value in (("edit_interval", 0.1), ("buffer_threshold", 1)):
        if key not in streaming:
            assign(streaming, key, value, f"streaming.{key}")
    if streaming.get("transport") == "off":
        assign(streaming, "transport", "edit", "streaming.transport")
    if feishu.get("streaming") is False:
        assign(feishu, "streaming", True, "display.platforms.feishu.streaming")
    return config, changes
