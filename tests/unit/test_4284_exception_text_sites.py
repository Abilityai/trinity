"""abilityai/trinity#4284: the 5xx sites the static guard found that still
rendered exception text or relayed an upstream body.

Each site is driven through its real function with a planted exception (or an
upstream answer) whose text carries a harmless marker and a credential: a
GitHub token in a remote URL's userinfo and in a ``key=value`` pair. The
answer must keep its status and prefix, name the exception class (or the
upstream status) and carry neither the marker nor the credential. Where the
site logs, the log must carry the marker, so the operator keeps the
diagnostic, and never the credential.

``test_4284_no_exception_text_in_5xx.py`` keeps new sites out; this file
proves the sites it found are fixed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_BASE_IMAGE = _REPO / "docker" / "base-image"

# Built by concatenation so no source line, and so no traceback frame, holds it.
_TOKEN = "ghp_" + "S14SENTINEL" + "a" * 25
_MARKER = "boom-marker-4284"
_PLANT = (
    f"{_MARKER} https://x-access-token:{_TOKEN}@github.com/acme/repo.git "
    f"token={_TOKEN}"
)


def _rendered(record: logging.LogRecord) -> str:
    return logging.Formatter("%(message)s").format(record)


def _assert_answer_clean(answer, expected: str) -> None:
    text = str(answer)
    assert _TOKEN not in text, f"credential reached the answer: {text!r}"
    assert _MARKER not in text, f"exception text reached the answer: {text!r}"
    assert expected in text, f"{expected!r} missing from the answer: {text!r}"


def _assert_log_keeps_text(records, needle: str) -> None:
    rendered = [_rendered(r) for r in records]
    for line in rendered:
        assert _TOKEN not in line, f"credential reached the log: {line!r}"
    hits = [line for line in rendered if needle in line and _MARKER in line]
    assert hits, f"no {needle!r} record carries the exception text: {rendered!r}"


# ---------------------------------------------------------------------------
# agent server: docker/base-image/agent_server/routers/git.py
# ---------------------------------------------------------------------------

# The router's imports are relative, so it is imported through the
# `agent_server` package, the way the agent server loads it (the test_2742
# loader and its snapshot/restore pair).
_BASE_IMAGE_STR = str(_BASE_IMAGE)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)
_existing = sys.modules.get("agent_server")
_real_path = str(_BASE_IMAGE / "agent_server")
if _existing is None or not any(
    _real_path in p for p in (getattr(_existing, "__path__", None) or [])
):
    for _mod in list(sys.modules):
        if _mod == "agent_server" or _mod.startswith("agent_server."):
            sys.modules.pop(_mod, None)
    _as_spec = importlib.util.spec_from_file_location(
        "agent_server", str(_BASE_IMAGE / "agent_server" / "__init__.py"),
        submodule_search_locations=[_real_path],
    )
    _as_mod = importlib.util.module_from_spec(_as_spec)
    sys.modules["agent_server"] = _as_mod
    _as_spec.loader.exec_module(_as_mod)

from agent_server.routers import git as agent_git_mod  # noqa: E402
from agent_server.services import process_registry as agent_registry  # noqa: E402

_STUBBED_MODULE_NAMES = [
    name for name in sys.modules
    if name == "agent_server" or name.startswith("agent_server.")
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture
def agent_git(tmp_path, monkeypatch):
    git_mod = agent_git_mod
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    real_path = Path
    monkeypatch.setattr(
        git_mod, "Path",
        lambda *a: real_path(home) if a == ("/home/developer",) else real_path(*a))

    def boom(*_a, **_kw):
        raise RuntimeError(_PLANT)

    monkeypatch.setattr(git_mod, "subprocess", types.SimpleNamespace(
        run=boom, TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError, PIPE=subprocess.PIPE))
    monkeypatch.setattr(git_mod, "run_registered", boom)
    return git_mod, home


def _agent_git_call(git_mod, home, route: str):
    if route == "status":
        return lambda: git_mod._compute_git_status(home)
    if route == "sync":
        return lambda: asyncio.run(git_mod.sync_to_github(git_mod.GitSyncRequest()))
    if route == "log":
        return lambda: asyncio.run(git_mod.get_git_log(limit=5))
    return lambda: asyncio.run(git_mod.pull_from_github(git_mod.GitPullRequest()))


@pytest.mark.parametrize("route", ["status", "sync", "log", "pull"])
def test_agent_git_500_names_the_class_not_the_text(agent_git, caplog, route):
    git_mod, home = agent_git
    caplog.set_level(logging.ERROR)
    with pytest.raises(HTTPException) as exc:
        _agent_git_call(git_mod, home, route)()
    assert exc.value.status_code == 500
    _assert_answer_clean(exc.value.detail, f"Git {route} error: RuntimeError")
    assert isinstance(exc.value.__cause__, RuntimeError)
    _assert_log_keeps_text(caplog.records, f"Git {route} error")


# ---------------------------------------------------------------------------
# agent server: services/process_registry.py feeds the terminate route's 500
# ---------------------------------------------------------------------------

def test_registry_terminate_error_names_the_class_not_the_text(monkeypatch, caplog):
    def boom(*_a, **_kw):
        raise RuntimeError(_PLANT)

    monkeypatch.setattr(agent_registry, "_signal_process_tree", boom)
    reg = agent_registry.ProcessRegistry()
    proc = SimpleNamespace(poll=lambda: None, pid=4242, returncode=None)
    reg._processes["exec-1"] = {"process": proc, "metadata": {"pgid": 4242}}
    caplog.set_level(logging.ERROR)
    result = reg.terminate("exec-1")
    assert result["success"] is False and result["reason"] == "error"
    _assert_answer_clean(result["error"], "Termination failed: RuntimeError")
    _assert_log_keeps_text(caplog.records, "exec-1")
