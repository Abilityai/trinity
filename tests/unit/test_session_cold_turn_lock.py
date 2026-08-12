"""
Issue #779 regression pin — cold-turn POSTs must serialize on the same session.

Before the fix, `_ResumeLock.__init__` short-circuited to `self._key = None`
when `claude_session_id` was None (the cold-turn path), so the
`async with _ResumeLock(...)` block was a no-op. Two concurrent first-turn
POSTs on a fresh session would bypass the lock, race on
`update_cached_claude_session_id`, and orphan one JSONL inside the agent
container (Anthropic claude-code #20992).

The fix keys the cold-turn lock on the session id
(`session_lock:cold:{session_id}`), so cold turns on the *same* session
block each other while cold turns on *different* sessions still run in
parallel.

This file pins:
1. ``_ResumeLock.__init__`` takes ``session_id`` as a third required parameter.
2. The cold-turn branch produces ``session_lock:cold:{session_id}`` — not
   ``None``, not anything else.
3. The warm-turn branch still uses the existing
   ``session_lock:{agent}:{uuid}`` format.
4. The single call site passes ``session.id`` as the third argument.
5. ``self._key`` is no longer treated as nullable in ``__aenter__`` /
   ``__aexit__`` (the dead-code guards are gone — they would otherwise
   silently bypass the new cold-turn lock if some future refactor revived
   a ``None`` key path).

Style follows ``tests/unit/test_session_persistence_flag.py``: AST + regex
on source, no live backend required.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SESSIONS_ROUTER_PY = _PROJECT_ROOT / "src" / "backend" / "routers" / "sessions.py"
# ent#358: the resume engine (ResumeLock and friends) moved out of the router
# into a service shared with Workspace chat. Engine assertions read the service;
# call-site assertions still read the router.
_TURN_SERVICE_PY = _PROJECT_ROOT / "src" / "backend" / "services" / "session_turn_service.py"


# ---------------------------------------------------------------------------
# Helpers — mirror test_session_persistence_flag.py
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    assert path.is_file(), f"missing {path}"
    return path.read_text()


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} not found")


def _find_method(cls: ast.ClassDef, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in cls.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"method {name} not found on {cls.name}")


def _function_source(source: str, func) -> str:
    lines = source.splitlines()
    return "\n".join(lines[func.lineno - 1 : func.end_lineno])


# ---------------------------------------------------------------------------
# 1. Constructor signature
# ---------------------------------------------------------------------------


def test_resume_lock_init_requires_session_id():
    """``__init__`` must accept ``session_id`` so cold turns have a stable key.

    Before #779: ``def __init__(self, agent_name, claude_session_id)``.
    After  #779: ``def __init__(self, agent_name, claude_session_id, session_id)``.
    """
    tree = ast.parse(_read(_TURN_SERVICE_PY))
    cls = _find_class(tree, "ResumeLock")
    init = _find_method(cls, "__init__")

    arg_names = [a.arg for a in init.args.args]
    assert "session_id" in arg_names, (
        f"_ResumeLock.__init__ must take session_id; got {arg_names!r}"
    )
    assert arg_names[:4] == ["self", "agent_name", "claude_session_id", "session_id"], (
        f"_ResumeLock.__init__ positional args drifted; got {arg_names!r}"
    )


# ---------------------------------------------------------------------------
# 2. Cold-turn key formula
# ---------------------------------------------------------------------------


def test_resume_lock_cold_turn_uses_session_id_key():
    """The cold-turn branch must build ``session_lock:cold:{session_id}``.

    The bug shape we never want to see again: the cold-turn branch falling
    back to ``None`` (or any other key that doesn't include session_id),
    which would silently disable the lock for the very race window this
    class exists to prevent (#779).
    """
    src = _read(_TURN_SERVICE_PY)

    # Positive: the cold-turn key literal is present, parameterised by session_id.
    assert re.search(
        r'f["\']session_lock:cold:\{session_id\}["\']',
        src,
    ), "cold-turn lock key must be f'session_lock:cold:{session_id}'"

    # Negative: no path leaves ``self._key`` as ``None``. Any future refactor
    # that wires a None-key fallback back into _ResumeLock would re-open #779.
    tree = ast.parse(src)
    cls = _find_class(tree, "ResumeLock")
    init = _find_method(cls, "__init__")
    init_src = _function_source(src, init)
    assert not re.search(r"self\._key\s*=\s*None", init_src), (
        "self._key must never be set to None — that re-enables the #779 race"
    )
    # And in the conditional, `else None` shouldn't appear as the alternate
    # branch for self._key.
    assert not re.search(
        r"self\._key\s*=\s*\([^)]*\belse\s+None\s*\)",
        init_src,
        flags=re.DOTALL,
    ), "self._key cold-turn branch must not be `else None`"


def test_resume_lock_warm_turn_key_unchanged():
    """The warm-turn key shape stays the same — fix is purely additive."""
    src = _read(_TURN_SERVICE_PY)
    assert re.search(
        r'f["\']session_lock:\{agent_name\}:\{claude_session_id\}["\']',
        src,
    ), "warm-turn key must remain f'session_lock:{agent_name}:{claude_session_id}'"


# ---------------------------------------------------------------------------
# 3. Call-site passes session.id
# ---------------------------------------------------------------------------


def test_message_endpoint_passes_session_id_to_resume_lock():
    """The cold-turn lock key must stay bound to the persisted session row.

    ent#358 split this across two files — the engine constructs the lock, the
    caller names the session — so both halves are pinned. The engine must still
    have exactly ONE construction site: a second one would be a second key
    convention, which is the split-brain this guard exists to prevent.
    """
    engine_src = _read(_TURN_SERVICE_PY)
    call_sites = re.findall(r"[^_\w]ResumeLock\([^)]+\)", engine_src)
    assert len(call_sites) == 1, (
        f"expected exactly one ResumeLock(...) call site in the engine; "
        f"found {len(call_sites)}: {call_sites!r}"
    )
    assert "session_key" in call_sites[0], (
        f"ResumeLock call must pass the caller's session key (third arg); "
        f"got {call_sites[0]!r}"
    )

    router_src = _read(_SESSIONS_ROUTER_PY)
    assert "session_key=session.id" in router_src, (
        "send_session_message must bind the lock to the persisted session row "
        "by passing session_key=session.id"
    )


# ---------------------------------------------------------------------------
# 4. Dead-code guards on the None path are gone
# ---------------------------------------------------------------------------


def test_aenter_no_longer_short_circuits_on_none_key():
    """``__aenter__`` previously had ``if self._key is None: return self`` —
    the no-op cold-turn path. With the fix, self._key is never None, so the
    guard is dead code AND would silently break the new cold-turn lock if
    reintroduced. Pin its absence."""
    src = _read(_TURN_SERVICE_PY)
    tree = ast.parse(src)
    cls = _find_class(tree, "ResumeLock")
    aenter = _find_method(cls, "__aenter__")
    body = _function_source(src, aenter)
    assert "self._key is None" not in body, (
        "__aenter__ must not short-circuit on self._key is None — #779 regression"
    )


def test_aexit_no_longer_short_circuits_on_none_key():
    """Same for ``__aexit__``."""
    src = _read(_TURN_SERVICE_PY)
    tree = ast.parse(src)
    cls = _find_class(tree, "ResumeLock")
    aexit = _find_method(cls, "__aexit__")
    body = _function_source(src, aexit)
    assert "self._key is None" not in body, (
        "__aexit__ must not short-circuit on self._key is None — #779 regression"
    )
