"""Hermes native registration; retain all built-in Feishu platform metadata."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

NATIVE_HOOKS = {
    "on_session_start": "observe_start",
    "pre_api_request": "observe_start",
    "on_stream_delta": "observe_reasoning",
    "pre_tool_call": "observe_tool_start",
    "post_tool_call": "observe_tool_end",
    "post_api_request": "observe_usage",
    "on_session_end": "observe_end",
    "on_human_input_request": "observe_human_request",
    "on_human_input_resolved": "observe_human_resolved",
    "agent_loop_stopped": "observe_stop",
}
TARGET_REVISION = "3b421578588243f920886e6fe79a6faa87ea026e"


def legacy_files(root: Path) -> list[Path]:
    paths = [*(root / "gateway").glob("run*.py"), *(root / "cron").glob("scheduler*.py")]
    paths.append(root / "plugins/platforms/feishu/adapter.py")
    return [path for path in paths if path.is_file() and "# HERMES_LARK_" in path.read_text()]


def check_host() -> None:
    from hermes_cli import plugins  # type: ignore[import-not-found]

    missing = set(NATIVE_HOOKS) - plugins.VALID_HOOKS
    if missing or not hasattr(plugins.PluginContext, "register_platform"):
        raise RuntimeError(f"Hermes native interfaces unavailable: {sorted(missing)}")
    # Registration can run on a deadline worker while the main thread imports
    # gateway.run. Importing it here would wait on the caller's import lock.
    root = Path(inspect.getfile(plugins)).resolve().parent.parent
    from plugins.platforms.feishu.adapter import FeishuAdapter  # type: ignore[import-not-found]

    from .native_contract import adapter_contract_errors

    errors = adapter_contract_errors(FeishuAdapter)
    if errors:
        raise RuntimeError("Hermes adapter interfaces unavailable: " + "; ".join(errors))
    residue = legacy_files(root)
    if residue:
        raise RuntimeError("Legacy injection remains; use the saved old plugin to remove it first: "
                           + ", ".join(str(p) for p in residue))


def register(ctx: Any) -> None:
    check_host()
    from plugins.platforms.feishu import adapter as builtin  # type: ignore[import-not-found]

    from . import native_hooks
    from .native_adapter import FeishuStreamingAdapter, standalone_send

    class Registration:
        def __getattr__(self, name: str) -> Any:
            return getattr(ctx, name)

        def register_platform(self, **kwargs: Any) -> Any:
            kwargs.update(adapter_factory=FeishuStreamingAdapter, standalone_sender_fn=standalone_send)
            return ctx.register_platform(**kwargs)

    builtin.register(Registration())
    for name, callback in NATIVE_HOOKS.items():
        ctx.register_hook(name, getattr(native_hooks, callback))
