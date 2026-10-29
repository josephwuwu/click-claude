"""Tracking of the currently active :class:`Context`.

Contexts push themselves onto a thread-local stack while they are in use,
which lets helpers like :func:`get_current_context` find them without the
context being passed around explicitly.
"""

from __future__ import annotations

import typing as t
from threading import local

if t.TYPE_CHECKING:
    from .core import Context

_state = local()


def _stack() -> list[Context]:
    try:
        return _state.stack  # type: ignore[no-any-return]
    except AttributeError:
        _state.stack = []
        return _state.stack  # type: ignore[no-any-return]


@t.overload
def get_current_context(silent: t.Literal[False] = False) -> Context: ...


@t.overload
def get_current_context(silent: bool = ...) -> Context | None: ...


def get_current_context(silent: bool = False) -> Context | None:
    """Return the innermost active context.

    :param silent: return ``None`` instead of raising :exc:`RuntimeError`
        when no context is active.
    """
    stack = _stack()
    if stack:
        return stack[-1]
    if silent:
        return None
    raise RuntimeError("There is no active click context.")


def push_context(ctx: Context) -> None:
    """Make ``ctx`` the current context."""
    _stack().append(ctx)


def pop_context() -> None:
    """Remove the current context from the stack."""
    _stack().pop()


def resolve_color_default(color: bool | None = None) -> bool | None:
    """Return ``color`` if given, otherwise the color setting of the current
    context, if any.
    """
    if color is not None:
        return color
    ctx = get_current_context(silent=True)
    if ctx is not None:
        return ctx.color
    return None
