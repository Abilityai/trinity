"""#2433 — headless subprocess runs use a DEDICATED thread pool, and a
cancel-while-pending never spends a subprocess.

- The pool is sized to the backend's `max_parallel_tasks` fleet ceiling
  (`settings_service.MAX_PARALLEL_TASKS_CEILING_MAX`, #506), pinned across the
  two trees by parsing the backend source (the agent server cannot import
  `src/backend` — Invariant #5). A raised ceiling that outgrows the pool would
  silently re-create the queue this fixes.
- `run_in_executor` is called with that pool, never the CPU-sized default
  (`None`) — a source guard, since `_setup_headless_command` is too heavy for a
  full async harness (the #1804/#1871 static-pin precedent).
- `_run_headless_subprocess` raises HTTPException(409) BEFORE `Popen` when the
  #679 cancel marker is set for the execution.

Module under test: docker/base-image/agent_server/services/headless_executor.py
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from agent_server.services import headless_executor as he  # noqa: E402

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS_SRC = _ROOT / "src" / "backend" / "services" / "settings_service.py"
_HE_SRC = _ROOT / "docker" / "base-image" / "agent_server" / "services" / "headless_executor.py"


def _backend_ceiling_max() -> int:
    tree = ast.parse(_SETTINGS_SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "MAX_PARALLEL_TASKS_CEILING_MAX":
                    return ast.literal_eval(node.value)
    raise AssertionError("MAX_PARALLEL_TASKS_CEILING_MAX not found in settings_service.py")


def test_pool_is_sized_to_the_backend_fleet_ceiling():
    assert he.HEADLESS_EXECUTOR_MAX_WORKERS == _backend_ceiling_max()
    assert he._HEADLESS_EXECUTOR._max_workers == he.HEADLESS_EXECUTOR_MAX_WORKERS


def test_pool_is_its_own_named_executor():
    assert he._HEADLESS_EXECUTOR._thread_name_prefix == "headless-task"


def test_headless_runs_go_to_the_dedicated_pool_not_the_default():
    src = _HE_SRC.read_text()
    assert "run_in_executor(_HEADLESS_EXECUTOR, _run_headless_subprocess, ctx)" in src
    assert re.search(r"run_in_executor\(\s*None\s*,\s*_run_headless_subprocess", src) is None, (
        "the headless subprocess run must never go back to the CPU-sized default executor"
    )


def _ctx():
    ctx = MagicMock()
    ctx.task_session_id = "exec-1"
    ctx.cmd = ["claude", "--print"]
    ctx.effective_timeout = 60.0
    ctx.prompt = "hello"
    return ctx


def test_pre_spawn_cancel_raises_409_without_spawning(monkeypatch):
    registry = MagicMock()
    registry.was_terminated.return_value = True
    monkeypatch.setattr(he, "get_process_registry", lambda: registry)

    def _no_popen(*a, **k):
        raise AssertionError("Popen must not run for a cancelled-before-start execution")

    monkeypatch.setattr(he.subprocess, "Popen", _no_popen)
    with pytest.raises(HTTPException) as ei:
        he._run_headless_subprocess(_ctx())
    assert ei.value.status_code == 409
    registry.was_terminated.assert_called_once_with("exec-1")


def test_uncancelled_run_reaches_popen(monkeypatch):
    class _Sentinel(Exception):
        pass

    registry = MagicMock()
    registry.was_terminated.return_value = False
    monkeypatch.setattr(he, "get_process_registry", lambda: registry)

    def _popen(*a, **k):
        raise _Sentinel()

    monkeypatch.setattr(he.subprocess, "Popen", _popen)
    with pytest.raises(_Sentinel):
        he._run_headless_subprocess(_ctx())
