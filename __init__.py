"""Directory-plugin entry point for the Hermes plugin manager."""

from typing import Any


def register(ctx: Any) -> None:
    from hermes_lark_streaming.native_plugin import register as register_native

    register_native(ctx)
