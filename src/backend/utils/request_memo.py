"""A memo for the lookups one read repeats (#4671, #4672).

The user, owner and access helpers each open a connection and run their own
SELECT on every call. A list that asks "who is this viewer" once per row, or
"may they reach this agent" once per agent, therefore runs a number of queries
that grows with what it returns — 130 extra lookups for 63 asks on the
instance that reported #4671.

This is the shared fix: inside a `scope()` a `@memoized` function runs once
per distinct arguments, and every later call gets the first answer. The rules
themselves are untouched — `can_user_access_agent` still decides access, it
just stops re-reading the same user and owner rows to do it — so no caller
re-implements membership to make a list fast.

Three properties are the contract; `tests/unit/test_4671_request_memo.py`
pins each:

1. **Opt-in and bounded.** Outside a `scope()` a memoized function is the
   plain function: no cache, no behaviour change. A scope is opened by the
   READ path that needs it and ends with it; the memo is a local dict dropped
   on exit, so nothing outlives the read. Do not open one around code that
   writes and then re-reads what it wrote — it would get the pre-write answer.
2. **Never a cross-principal answer.** The key is the function plus every
   argument, so the answer for one user or agent is never returned for
   another, and a scope belongs to one call chain (a `ContextVar`): two
   requests never share a dict.
3. **Failures are not remembered.** An exception propagates and is retried by
   the next call, so a fail-closed check keeps failing closed per call rather
   than replaying one cached outcome. A `None` / `False` answer IS remembered —
   "no such user" is an answer.

Answers are shared by reference: treat what a memoized function returns as
read-only inside a scope.
"""
from __future__ import annotations

import functools
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Dict, Hashable, Iterator, Optional, Tuple, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

_active: ContextVar[Optional[Dict[Hashable, Any]]] = ContextVar("request_memo", default=None)


@contextmanager
def scope() -> Iterator[None]:
    """Memoize every `@memoized` call made inside the block.

    Re-entrant: a scope opened inside another joins it, and the memo is
    dropped only when the outermost one exits.
    """
    if _active.get() is not None:
        yield
        return
    token = _active.set({})
    try:
        yield
    finally:
        _active.reset(token)


def scoped(fn: F) -> F:
    """Run `fn` inside a `scope()`."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with scope():
            return fn(*args, **kwargs)
    return wrapper  # type: ignore[return-value]


def active() -> bool:
    return _active.get() is not None


def _key(fn: Callable, args: Tuple, kwargs: Dict[str, Any]) -> Optional[Hashable]:
    key = (fn, args, tuple(sorted(kwargs.items())))
    try:
        hash(key)
    except TypeError:
        return None  # an unhashable argument: just call through
    return key


def _memoize(fn: Callable, skip: int) -> Callable:
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        memo = _active.get()
        key = _key(fn, args[skip:], kwargs) if memo is not None else None
        if key is None:
            return fn(*args, **kwargs)
        if key in memo:
            return memo[key]
        value = fn(*args, **kwargs)
        memo[key] = value
        return value

    def prime(*args, value: Any) -> None:
        """Record the answer for `args` (without `self`) when a batch read
        already has it. A no-op outside a scope, and never overwrites an
        answer the scope already holds."""
        memo = _active.get()
        key = _key(fn, args, {}) if memo is not None else None
        if key is not None:
            memo.setdefault(key, value)

    wrapper.prime = prime  # type: ignore[attr-defined]
    return wrapper


def memoized(fn: F) -> F:
    """Memoize a plain function inside a `scope()`."""
    return _memoize(fn, 0)  # type: ignore[return-value]


def memoized_method(fn: F) -> F:
    """Memoize a method of a process-wide singleton (the `db/` operation
    classes): `self` is left out of the key."""
    return _memoize(fn, 1)  # type: ignore[return-value]
