"""trinity-enterprise#704 — the git status reports the binding.

The Git panel badge ("Agent · own branch" / "Pull-only") reads
`db_config.source_mode` from `GET /api/agents/{name}/git/status`. The handler
is called directly with the git service patched at the router's module binding.
"""
from __future__ import annotations

import asyncio
import types
from datetime import datetime, timezone

import pytest

import routers.git as git_router
from db_models import AgentGitConfig

pytestmark = pytest.mark.unit


def _config(source_mode: bool, auto_sync_enabled: bool = False) -> AgentGitConfig:
    return AgentGitConfig(
        id="c1", agent_name="a1", github_repo="acme/tool",
        working_branch="main" if source_mode else "trinity/a1/x",
        instance_id="x", source_mode=source_mode,
        auto_sync_enabled=auto_sync_enabled,
        created_at=datetime.now(timezone.utc),
    )


def _status(monkeypatch, config):
    async def live_status(_name):
        return {"git_enabled": True, "branch": "main"}

    monkeypatch.setattr(
        git_router, "git_service",
        types.SimpleNamespace(
            get_agent_git_config=lambda _name: config,
            get_git_status=live_status,
        ),
    )
    return asyncio.run(git_router.get_git_status("a1", request=None))


@pytest.mark.parametrize("source_mode", [True, False])
def test_git_status_db_config_carries_the_binding(monkeypatch, source_mode):
    async def live_status(_name):
        return {"git_enabled": True, "branch": "main"}

    monkeypatch.setattr(
        git_router, "git_service",
        types.SimpleNamespace(
            get_agent_git_config=lambda _name: _config(source_mode),
            get_git_status=live_status,
        ),
    )
    status = asyncio.run(git_router.get_git_status("a1", request=None))
    assert status["db_config"]["source_mode"] is source_mode


@pytest.mark.parametrize("source_mode, auto_sync, pushes", [
    (False, True, True),    # a working branch with auto-sync on
    (False, False, True),   # a working branch, auto-sync toggled off: still its own branch
    (True, True, True),     # fork-to-own: source mode on its own fork, auto-pushing
    (True, False, False),   # a deployment / pull-only agent
], ids=["working-branch", "working-branch-paused", "fork-to-own", "pull-only"])
def test_git_status_says_whether_the_agent_pushes(monkeypatch, source_mode, auto_sync, pushes):
    """PR #3022 review: `source_mode` alone called every fork-to-own agent
    pull-only. `pushes` is what the badge keys on."""
    status = _status(monkeypatch, _config(source_mode, auto_sync))
    assert status["db_config"]["pushes"] is pushes
