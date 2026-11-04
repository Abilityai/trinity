"""`GET /api/agents/{name}` says whether a repository is bound (#3872).

The agent header reserves its GitHub row at first paint so the live status
(an exec into the running container, seconds later) cannot push the tab strip
down when it lands. It used to reserve on `agent.template.startsWith('github:')`,
which is false for an agent deployed locally and bound afterwards (a `local:`
label, or an empty one — #1811), so for those the row popped in. The persisted
`agent_git_config` row is the honest first-paint signal — it survives stops and
recovery, and `lifecycle._reconstruct_template_id` already treats it as what
makes an agent GitHub-backed — so the detail payload carries it as
`git_configured`, one read like `display_label` (not the list path).

Real DB via db_harness; the Docker-backed lookup is stubbed.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from db_harness import db_backend, run as _hrun, seed_user  # noqa: F401

pytestmark = pytest.mark.unit


@pytest.fixture
def db(db_backend):  # noqa: F811
    from database import db as database
    return database


def _seed_agent(name: str) -> None:
    _hrun(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at) "
        "VALUES (:n, 1, '2026-01-01T00:00:00Z', NULL)",
        n=name,
    )


def _detail(monkeypatch, name: str) -> dict:
    import config
    import routers.agents as agents_router
    from models import User

    monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", False)
    # A local-template agent: the template string says nothing about a repo.
    monkeypatch.setattr(
        agents_router, "get_agent_by_name",
        lambda n: {
            "name": n, "status": "running", "port": 2222, "created": "2026-01-01T00:00:00Z",
            "resources": {}, "container_id": "c", "template": "local:scout",
            "runtime": "claude-code",
        },
    )
    user = User(id=1, username="owner", role="user")
    return asyncio.run(agents_router.get_agent_endpoint(
        agent_name=name, request=SimpleNamespace(), current_user=user,
    ))


def test_a_bound_local_template_agent_reports_git_configured(db, monkeypatch):
    seed_user(1, "owner", "user")
    _seed_agent("bound")
    db.create_git_config(
        agent_name="bound", github_repo="owner/bound",
        working_branch="trinity/bound/x", instance_id="inst-bound",
    )

    body = _detail(monkeypatch, "bound")

    assert body["template"] == "local:scout"   # the template still says nothing...
    assert body["git_configured"] is True      # ...the payload says the repo is bound


def test_an_unbound_agent_reports_git_configured_false(db, monkeypatch):
    """`False`, never missing: the header must be able to tell "no repo" from
    "an older backend that does not carry the field"."""
    seed_user(1, "owner", "user")
    _seed_agent("plain")

    body = _detail(monkeypatch, "plain")

    assert body["git_configured"] is False
