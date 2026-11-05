"""abilityai/trinity-enterprise#1117 — the backend refuses a sync `paths`
element that git would read as an option.

The agent server builds `git add -- <paths>`, but an agent keeps the agent
server it was created with until it is recreated on a new base image. Until
then its Push still runs `git add <paths>`, where `paths: ["-f", ".env"]`
force-stages the gitignored credentials file and pushes it. The backend is the
edge every caller crosses (UI, API key, the `git_sync` MCP tool), so it refuses
any element starting with `-` with a 422 that names the element, before the
agent is ever called. A dash anywhere else (`sub/-x.md`) is an ordinary path.

The route tests mount the REAL `routers.git` router with the platform's real
422 handler (`error_handlers.validation_error_without_input`, which drops the
rejected `input`, so the element must be named in `msg` to reach the caller).
Only auth, the container lookup, the agent call and the audit write are stubbed.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Same unit-island preamble as test_2529_gitignore_precedence.py::_git_router:
# `routers.git` pulls config.py (raises without Redis credentials) and database.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault(
    "TRINITY_DB_PATH",
    str(Path(tempfile.gettempdir()) / "trinity_test_ent1117_s6a.db"),
)

from pydantic import ValidationError  # noqa: E402

from models import GitSyncRequest  # noqa: E402


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "paths, bad",
    [
        (["-f", ".env"], "-f"),
        ([".env", "--force"], "--force"),
        (["notes.md", "--pathspec-from-file=/etc/passwd"], "--pathspec-from-file=/etc/passwd"),
        (["-"], "-"),
    ],
)
def test_model_rejects_an_element_starting_with_a_dash_and_names_it(paths, bad):
    with pytest.raises(ValidationError) as exc:
        GitSyncRequest(paths=paths)
    errors = exc.value.errors()
    assert len(errors) == 1, errors
    assert errors[0]["loc"][0] == "paths"
    assert repr(bad) in errors[0]["msg"], errors[0]["msg"]


@pytest.mark.parametrize(
    "paths",
    [
        ["sub/-x.md"],
        ["notes.md", "a-b/c-d.txt", "./-leading-dash-spelt-safely.md"],
        ["x-"],
    ],
)
def test_model_accepts_a_dash_anywhere_but_the_first_character(paths):
    assert GitSyncRequest(paths=paths).paths == paths


@pytest.mark.parametrize("kwargs", [{}, {"paths": None}, {"paths": []}])
def test_characterization_no_paths_is_unchanged(kwargs):
    """Characterization (green before and after): omitting `paths`, sending
    null, or an empty list still validates and reaches the agent as falsy,
    which the agent server reads as `git add -A`."""
    assert not GitSyncRequest(**kwargs).paths


# ---------------------------------------------------------------------------
# The real route: 422 before the agent is called
# ---------------------------------------------------------------------------

def _client(sync_mock):
    from fastapi import FastAPI
    from fastapi.exceptions import RequestValidationError
    from fastapi.testclient import TestClient

    import typing

    import routers.git as git_router
    from error_handlers import validation_error_without_input

    # Override the dependency objects the mounted router actually holds. The
    # unit conftest restores sys.modules between tests, so a fresh
    # `from dependencies import ...` can name a different function object than
    # the one this router module captured at its import.
    owned_agent_dep = typing.get_args(git_router.OwnedAgentByName)[1].dependency

    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_error_without_input)
    app.include_router(git_router.router)
    app.dependency_overrides[owned_agent_dep] = lambda: "alpha"
    app.dependency_overrides[git_router.get_current_user] = lambda: type(
        "U", (), {"id": 1, "username": "alice", "email": "alice@example.com",
                  "role": "admin", "agent_name": None}
    )()
    patches = [
        patch.object(git_router.git_service, "sync_to_github", new=sync_mock),
        patch.object(git_router, "_audit_git", new=AsyncMock()),
        patch("services.docker_service.get_agent_container", return_value=object()),
    ]
    return TestClient(app), patches


def _ok_result():
    from database import GitSyncResult

    return GitSyncResult(success=True, message="Synced", commit_sha="abc", files_changed=1)


def test_route_answers_422_naming_the_element_and_never_calls_the_agent():
    sync_mock = AsyncMock(return_value=_ok_result())
    client, patches = _client(sync_mock)
    with patches[0], patches[1], patches[2]:
        resp = client.post("/api/agents/alpha/git/sync", json={"paths": ["-f", ".env"]})

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert any("'-f'" in d["msg"] for d in detail), detail
    assert all("input" not in d for d in detail), "the 422 handler must still drop `input`"
    sync_mock.assert_not_awaited()


def test_route_forwards_a_dash_in_the_middle_unchanged():
    sync_mock = AsyncMock(return_value=_ok_result())
    client, patches = _client(sync_mock)
    with patches[0], patches[1], patches[2]:
        resp = client.post("/api/agents/alpha/git/sync", json={"paths": ["sub/-x.md"]})

    assert resp.status_code == 200, resp.text
    sync_mock.assert_awaited_once()
    assert sync_mock.await_args.kwargs["paths"] == ["sub/-x.md"]


def test_characterization_route_with_no_body_still_syncs_everything():
    """Characterization: an empty body still reaches the agent with paths=None."""
    sync_mock = AsyncMock(return_value=_ok_result())
    client, patches = _client(sync_mock)
    with patches[0], patches[1], patches[2]:
        resp = client.post("/api/agents/alpha/git/sync")

    assert resp.status_code == 200, resp.text
    assert sync_mock.await_args.kwargs["paths"] is None
