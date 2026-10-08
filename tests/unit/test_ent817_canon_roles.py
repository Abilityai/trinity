"""trinity-enterprise#817 — the seats an agent's canon defines.

`canon_roles_service.read_canon_roles(agent)` lists `<canon>/roles/*.yaml` with
each file's id (the stem), `title` and `updated` stamp, read from the agent's
own container through the objective join's agent-door helpers. It never raises,
and an empty list always says why: stopped, no canon, no `roles/`, unreadable,
too slow. Driven with the ent#666 suite's fake agent door.
"""
import pytest

from services import canon_roles_service as svc
from unit.test_ent666_objective_join import _FakeClient, running  # noqa: F401 — fixture

pytestmark = pytest.mark.unit

AGENT = "sales-companion"
CANON = "name: sales-companion\nx-canon:\n  clone_path: canon\n"


def _client(files, listing):
    return _FakeClient(files=files, listing=listing)


@pytest.mark.asyncio
async def test_it_lists_each_role_with_its_title_and_updated_stamp(running):
    client = _client({
        "template.yaml": CANON,
        "canon/roles/head-of-sales.yaml": "title: Head of Sales\nupdated: 2026-09-30\nmission: Grow\n",
        "canon/roles/cfo.yaml": "title: CFO\n",
    }, ["head-of-sales.yaml", "cfo.yaml", "README.md"])
    out = await svc.read_canon_roles(AGENT, client=client)

    assert out["unavailable"] is None and out["reason"] is None and out["canon_root"] == "canon"
    assert out["roles"] == [
        {"id": "cfo", "title": "CFO", "updated": None, "path": "canon/roles/cfo.yaml", "error": None},
        {"id": "head-of-sales", "title": "Head of Sales", "updated": "2026-09-30",
         "path": "canon/roles/head-of-sales.yaml", "error": None},
    ]
    assert any("roles" in c for c in client.calls if c.startswith("/api/files?"))


@pytest.mark.asyncio
async def test_a_file_that_will_not_parse_is_named_not_dropped(running):
    client = _client({"template.yaml": CANON, "canon/roles/broken.yaml": "title: [unclosed"},
                     ["broken.yaml"])
    out = await svc.read_canon_roles(AGENT, client=client)
    assert out["roles"] == [{"id": "broken", "title": None, "updated": None,
                             "path": "canon/roles/broken.yaml", "error": "invalid"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("template,reason", [
    ("name: plain-agent\n", "no_canon"),
    ("x-canon:\n  clone_path: ../../etc\n", "canon_path_invalid"),
], ids=["no-canon", "traversing-path"])
async def test_no_usable_canon_is_named(running, template, reason):
    client = _client({"template.yaml": template}, [])
    out = await svc.read_canon_roles(AGENT, client=client)
    assert out["roles"] == [] and out["reason"] == reason and out["message"]
    assert not any(c.startswith("/api/files?") for c in client.calls), "no listing without a canon"


@pytest.mark.asyncio
async def test_a_missing_roles_directory_is_named(running):
    client = _FakeClient(files={"template.yaml": CANON}, list_status=404)
    out = await svc.read_canon_roles(AGENT, client=client)
    assert out["roles"] == [] and out["reason"] == "absent" and "roles/" in out["message"]


@pytest.mark.asyncio
async def test_an_empty_roles_directory_says_so(running):
    out = await svc.read_canon_roles(AGENT, client=_client({"template.yaml": CANON}, []))
    assert out["roles"] == [] and out["reason"] == "empty"


@pytest.mark.asyncio
@pytest.mark.parametrize("state,unavailable", [
    ("stopped", "agent_stopped"), ("missing", "agent_missing"), (None, "agent_unreachable")])
async def test_an_agent_that_is_not_running_is_named(monkeypatch, state, unavailable):
    from services import docker_utils

    async def _state(name):
        return state
    monkeypatch.setattr(docker_utils, "agent_container_state_async", _state)
    out = await svc.read_canon_roles(AGENT, client=_client({}, []))
    assert out["unavailable"] == unavailable and out["roles"] == [] and out["message"]


@pytest.mark.asyncio
async def test_an_agent_that_stops_answering_is_named(running):
    client = _FakeClient(files={"template.yaml": CANON}, listing=["a.yaml"], fail_after=1)
    out = await svc.read_canon_roles(AGENT, client=client)
    assert out["unavailable"] == "agent_unreachable"


@pytest.mark.asyncio
async def test_too_many_files_are_capped_and_said(running, monkeypatch):
    monkeypatch.setattr(svc, "MAX_ROLES", 2)
    files = {"template.yaml": CANON, **{f"canon/roles/r{i}.yaml": f"title: R{i}\n" for i in range(3)}}
    out = await svc.read_canon_roles(AGENT, client=_client(files, [f"r{i}.yaml" for i in range(3)]))
    assert len(out["roles"]) == 2 and out["truncated"] is True


@pytest.mark.asyncio
async def test_a_slow_read_is_named_not_waited_on(running, monkeypatch):
    import asyncio

    class Slow(_FakeClient):
        async def get(self, path, timeout=None, **kw):
            await asyncio.sleep(5)
    monkeypatch.setattr(svc, "ROLES_READ_BUDGET_SEC", 0.05)
    out = await svc.read_canon_roles(AGENT, client=Slow())
    assert out["reason"] == "timeout" and out["roles"] == []


@pytest.mark.asyncio
async def test_an_agent_key_reads_only_its_own_canon(monkeypatch):
    from fastapi import HTTPException
    from models import User
    from routers import agent_files
    monkeypatch.setattr(agent_files.objectives_read_budget, "enforce", lambda *a, **k: None)
    with pytest.raises(HTTPException) as e:
        await agent_files.get_agent_canon_roles(
            agent_name="other-agent",
            current_user=User(id=1, username="admin", role="admin",
                              agent_name=AGENT, mcp_scope="agent"))
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_the_read_draws_on_the_objectives_budget(monkeypatch):
    from models import User
    from routers import agent_files
    spent = []
    monkeypatch.setattr(agent_files.objectives_read_budget, "enforce",
                        lambda agent, **k: spent.append(agent))

    async def roles(agent):
        return {"agent_name": agent, "roles": []}
    monkeypatch.setattr(agent_files.canon_roles_service, "read_canon_roles", roles)
    out = await agent_files.get_agent_canon_roles(
        agent_name=AGENT, current_user=User(id=1, username="admin", role="admin"))
    assert spent == [AGENT] and out["agent_name"] == AGENT
