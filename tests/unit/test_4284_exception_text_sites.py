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


# ---------------------------------------------------------------------------
# GitHub errors at 502: services/agent_service/crud.py and fork_to_own.py
#
# A GitHubError raised for a network failure embeds the httpx error text, and
# for a header-unsafe token httpx echoes the Authorization value there.
# ---------------------------------------------------------------------------

def _github_error(status=None):
    from services.github_service import GitHubError
    return GitHubError(f"Network error: {_PLANT}", status=status)


@pytest.fixture
def crud_gh(monkeypatch):
    import services.agent_service.crud as crud
    state = {"status": None, "probe": ("unavailable", "")}

    class _GH:
        def __init__(self, pat):
            self.pat = pat

        async def check_repo_exists(self, owner, name):
            raise _github_error(state["status"])

    async def probe(*_a, **_kw):
        return state["probe"]

    async def push(*_a, **_kw):
        return None

    monkeypatch.setattr(crud, "GitHubService", _GH)
    monkeypatch.setattr(crud.git_service, "probe_tokened_repo_access", probe)
    monkeypatch.setattr(crud, "_validate_push_access", push)

    def validate():
        config = types.SimpleNamespace(source_branch="dev")
        return asyncio.run(crud._validate_github_access(config, "acme/tpl", _TOKEN))

    return state, validate


@pytest.mark.parametrize("status, expected", [
    (None, "Failed to validate GitHub repository access (GitHubError"),
    (403, "Failed to validate GitHub repository access (GitHubError, HTTP 403"),
], ids=["network-error", "rest-403-and-git-cannot-read"])
def test_crud_github_502_names_the_class_not_the_text(crud_gh, caplog, status, expected):
    state, validate = crud_gh
    state["status"] = status
    caplog.set_level(logging.WARNING)
    with pytest.raises(HTTPException) as exc:
        validate()
    assert exc.value.status_code == 502
    _assert_answer_clean(exc.value.detail, expected)
    _assert_log_keeps_text(caplog.records, "GitHub repository access")


class _ForkGH:
    def __init__(self, pat, *, fail_on):
        self.pat = pat
        self.fail_on = fail_on

    async def check_repo_exists(self, owner, name):
        if self.fail_on == "check":
            raise _github_error()
        return types.SimpleNamespace(exists=True, default_branch="main")

    async def get_branch_sha(self, repo, branch):
        raise _github_error(503)

    async def list_branches(self, repo, limit=10):
        raise _github_error(503)


@pytest.mark.parametrize("fail_on, code, expected", [
    ("check", "FORK_TEMPLATE_UNREACHABLE", "Could not read template 'acme/tpl' (GitHubError"),
    ("branch", "FORK_TEMPLATE_UNREACHABLE", "Could not read template branch (GitHubError, HTTP 503"),
])
def test_fork_template_502_names_the_class_not_the_text(monkeypatch, caplog, fail_on, code, expected):
    import services.agent_service.fork_to_own as fork
    monkeypatch.setattr(fork, "GitHubService", lambda pat: _ForkGH(pat, fail_on=fail_on))
    caplog.set_level(logging.WARNING)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(fork._resolve_template_tip("acme/tpl", _TOKEN))
    assert exc.value.status_code == 502
    assert exc.value.detail["code"] == code
    _assert_answer_clean(exc.value.detail["error"], expected)
    _assert_log_keeps_text(caplog.records, "fork-to-own")


@pytest.mark.parametrize("fail_on, expected", [
    ("check", "Could not check 'me/dest' (GitHubError"),
    ("branches", "Could not inspect 'me/dest' (GitHubError, HTTP 503"),
])
def test_fork_destination_502_names_the_class_not_the_text(caplog, fail_on, expected):
    import services.agent_service.fork_to_own as fork
    caplog.set_level(logging.WARNING)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(fork.inspect_or_create_destination_repo(
            _ForkGH(_TOKEN, fail_on=fail_on), "me/dest", "me",
            private=True, description="d", user_pat=_TOKEN))
    assert exc.value.status_code == 502
    assert exc.value.detail["code"] == "FORK_DESTINATION_UNREACHABLE"
    _assert_answer_clean(exc.value.detail["error"], expected)
    _assert_log_keeps_text(caplog.records, "fork-to-own")


# ---------------------------------------------------------------------------
# services/agent_service/repo_binding.py: the destination pre-check
# (the recreate failure is pinned in test_ent109_repo_binding.py)
# ---------------------------------------------------------------------------

def test_repo_binding_destination_check_502_names_the_class(monkeypatch, caplog):
    import services.agent_service.repo_binding as rb

    def boom(_repo):
        raise RuntimeError(_PLANT)

    monkeypatch.setattr(rb.db, "get_git_config_agent_names_for_repo", boom)
    caplog.set_level(logging.WARNING)
    with pytest.raises(rb.BindError) as exc:
        rb._refuse_if_destination_bound_elsewhere("agent-a", "me/dest")
    assert exc.value.status_code == 502
    assert exc.value.code == "BIND_DESTINATION_UNREACHABLE"
    _assert_answer_clean(exc.value.message, "(RuntimeError — details in backend logs)")
    _assert_log_keeps_text(caplog.records, "me/dest")
