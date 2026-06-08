"""Task-shape demotion PR 1 (#1068) — per-task ``timeout_seconds`` deprecation.

The first of the six task-shape demotions from
``docs/planning/ACTOR_MODEL_TASK_DEMOTION_MAP.md``. ``ParallelTaskRequest``'s
per-task ``timeout_seconds`` override is deprecated: the agent's
``execution_timeout_seconds`` (#665) / schedule cap (#913, clamped to the agent
cap by #929) is authoritative. Until the field is removed (follow-up PR after
one release of soak), ``routers/chat.py:_resolve_deprecated_task_timeout``
honors it but clamps it to the agent cap and emits a deprecation warning.

These tests exercise that pure helper. They AST-extract the function from the
real ``routers/chat.py`` source and exec it in isolation — the same
import-free convention ``test_backlog.py`` uses for chat.py, avoiding the
router module's heavy dependency graph and proving the helper stays pure
(no module-level state beyond ``typing.Optional``).
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_CHAT_SRC = _BACKEND / "routers" / "chat.py"
_FUNC_NAME = "_resolve_deprecated_task_timeout"


def _load_helper():
    """AST-extract `_resolve_deprecated_task_timeout` from chat.py and exec it.

    Importing `routers.chat` would drag in FastAPI, the service layer, and the
    DB — none of which this pure function needs. Parsing + exec'ing just the
    one FunctionDef keeps the test hermetic and fails loudly if the helper ever
    grows a hidden module-level dependency.
    """
    assert _CHAT_SRC.exists(), f"routers/chat.py not found at {_CHAT_SRC}"
    tree = ast.parse(_CHAT_SRC.read_text(), filename=str(_CHAT_SRC))
    node = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == _FUNC_NAME),
        None,
    )
    assert node is not None, (
        f"{_FUNC_NAME} not defined in routers/chat.py — demotion PR 1 (#1068) "
        f"helper was renamed or removed without updating this test."
    )
    module = ast.Module(body=[node], type_ignores=[])
    ns: dict = {"Optional": Optional}
    exec(compile(module, filename=str(_CHAT_SRC), mode="exec"), ns)  # noqa: S102
    return ns[_FUNC_NAME]


@pytest.fixture(scope="module")
def resolve():
    return _load_helper()


class TestResolveDeprecatedTaskTimeout:
    """#1068: the agent cap is authoritative; the per-task override only clamps down."""

    def test_no_override_is_pass_through_no_warning(self, resolve):
        """None override → None resolved (execute_task falls back to agent cap), no warning."""
        resolved, warning = resolve(None, 3600)
        assert resolved is None
        assert warning is None

    def test_override_below_cap_is_honored_with_warning(self, resolve):
        """A sub-cap override is preserved (shorter deadline) but still warns."""
        resolved, warning = resolve(60, 3600)
        assert resolved == 60
        assert warning is not None
        assert "deprecated" in warning.lower()

    def test_override_above_cap_is_clamped_with_warning(self, resolve):
        """An over-cap override is clamped to the agent cap (closes the pre-#1068 escape)."""
        resolved, warning = resolve(7200, 3600)
        assert resolved == 3600
        assert warning is not None
        assert "clamp" in warning.lower()

    def test_override_equal_to_cap_is_honored_not_clamped(self, resolve):
        """Boundary: requested == cap is the honored branch, not the clamp branch."""
        resolved, warning = resolve(3600, 3600)
        assert resolved == 3600
        assert warning is not None
        assert "clamp" not in warning.lower()

    def test_resolved_value_never_exceeds_cap(self, resolve):
        """Invariant: the returned timeout is always <= the agent cap (when set)."""
        cap = 1800
        for requested in (1, 60, 1799, 1800, 1801, 5000, 7200):
            resolved, _ = resolve(requested, cap)
            assert resolved is not None and resolved <= cap, (
                f"requested={requested} resolved={resolved} exceeds cap={cap}"
            )
