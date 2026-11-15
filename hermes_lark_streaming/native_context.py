"""Use the owning profile for asynchronous card work and read-only host helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast

P = ParamSpec("P")
R = TypeVar("R")


@contextmanager
def profile_scope(home: Path) -> Iterator[None]:
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override  # type: ignore[import-not-found]
    except ImportError:
        # Pure controller regression tests do not import a host runtime.
        yield
        return
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def native_home(method: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @wraps(method)
    async def owned(*args: P.args, **kwargs: P.kwargs) -> R:
        owner = cast(Any, args[0])
        with profile_scope(owner._profile_home):
            return await method(*args, **kwargs)

    return owned
