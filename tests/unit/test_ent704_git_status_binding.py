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


def _config(source_mode: bool) -> AgentGitConfig:
    return AgentGitConfig(
        id="c1", agent_name="a1", github_repo="acme/tool",
        working_branch="main" if source_mode else "trinity/a1/x",
        instance_id="x", source_mode=source_mode,
        created_at=datetime.now(timezone.utc),
    )


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
