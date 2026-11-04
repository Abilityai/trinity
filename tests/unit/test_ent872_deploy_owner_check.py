"""trinity-enterprise#872 — deploy-local refuses a base name the caller may not manage.

`deploy_local_agent_logic` computes the existing versions of a base name and
stops the latest running one. Without an ownership check, any caller deploying
under another user's base name stopped that user's agent and created `<base>-N`
beside it; an admin-owned agent key could do the same to a sibling it did not
spawn. Drives the REAL function with the `test_2060_deploy_integrity.py`
harness.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import dependencies
from services.agent_service import deploy as deploy_mod
from unit.test_2060_deploy_integrity import (  # noqa: F401 — fixtures
    _body,
    _build_archive,
    _deploy,
    flow_stubs,
    templates_dir,
)

pytestmark = pytest.mark.unit

BASE = "shared-name"


def _person(username, role="creator"):
    return SimpleNamespace(username=username, email=f"{username}@example.com", role=role, id=1)


def _agent_key(agent_name, owner="admin"):
    return SimpleNamespace(
        username=owner, email=f"{owner}@example.com", role="admin", id=1,
        mcp_scope="agent", agent_name=agent_name,
    )


@pytest.fixture
def world(flow_stubs):
    """`BASE` exists, running, owned by `alice`; `admin` owns `parent`."""
    owners = {BASE: "alice", "parent": "admin"}
    existing = SimpleNamespace(name=BASE, status="running")
    stop = AsyncMock()
    created = []

    flow_stubs.setattr(deploy_mod, "get_agents_by_prefix", lambda b: [existing])
    flow_stubs.setattr(deploy_mod, "get_latest_version", lambda b: existing)
    flow_stubs.setattr(deploy_mod, "get_next_version_name", lambda b: f"{b}-2")
    flow_stubs.setattr(deploy_mod.db, "get_agents_by_owner",
                       lambda u: [n for n, o in owners.items() if o == u])
    flow_stubs.setattr(deploy_mod.db, "get_agent_owner",
                       lambda n: {"owner_username": owners.get(n)})
    roles = {"alice": "creator", "bob": "creator", "admin": "admin"}
    flow_stubs.setattr(
        deploy_mod.db, "can_user_share_agent",
        lambda u, n: owners.get(n) == u or roles.get(u) == "admin",
    )
    flow_stubs.setattr(deploy_mod, "get_agent_container", lambda n: SimpleNamespace(name=n))
    flow_stubs.setattr(deploy_mod, "container_stop", stop)
    flow_stubs.setattr(deploy_mod, "container_start", AsyncMock(), raising=False)
    flow_stubs.setattr(dependencies, "_has_edge", lambda a, t: False)
    flow_stubs.setattr(dependencies, "_spawned_by_caller", lambda a, t: False)

    async def create(config, *a, **kw):
        created.append(config.name)
        from unit.test_2060_deploy_integrity import _agent_status
        return _agent_status(config.name)

    return SimpleNamespace(stop=stop, created=created, create=create, mp=flow_stubs)


async def _run(world, user):
    return await _deploy(_body(_build_archive(BASE), BASE), user, world.create)


@pytest.mark.asyncio
async def test_other_user_cannot_deploy_over_a_name_they_do_not_own(world):
    with pytest.raises(HTTPException) as ei:
        await _run(world, _person("bob"))
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "NAME_OWNED_BY_ANOTHER_USER"
    world.stop.assert_not_called()
    assert world.created == []


@pytest.mark.asyncio
async def test_owner_redeploy_stops_previous_and_proceeds(world):
    await _run(world, _person("alice"))
    world.stop.assert_awaited_once()
    assert world.created == [f"{BASE}-2"]


@pytest.mark.asyncio
async def test_admin_person_redeploys_another_users_agent(world):
    await _run(world, _person("admin", role="admin"))
    world.stop.assert_awaited_once()
    assert world.created == [f"{BASE}-2"]


@pytest.mark.asyncio
async def test_admin_owned_agent_key_cannot_deploy_over_unspawned_sibling(world):
    with pytest.raises(HTTPException) as ei:
        await _run(world, _agent_key("parent"))
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "NAME_OWNED_BY_ANOTHER_USER"
    world.stop.assert_not_called()
    assert world.created == []


@pytest.mark.asyncio
async def test_admin_owned_agent_key_redeploys_its_spawned_child(world):
    world.mp.setattr(dependencies, "_spawned_by_caller",
                     lambda a, t: a == "parent" and t == BASE)
    await _run(world, _agent_key("parent"))
    world.stop.assert_awaited_once()
    assert world.created == [f"{BASE}-2"]
