from typing import Any

__version__ = "0.12.0"


def register(ctx: Any) -> None:
    """Lazy package entry point; importing business/controller code has no side effects."""
    from .native_plugin import register as register_native

    register_native(ctx)
