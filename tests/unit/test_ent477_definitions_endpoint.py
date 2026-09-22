"""Metric-definitions read + refresh routes — ent#477 (R5).

Two routes on `routers/agent_files.py`:

  * `GET  /api/agents/{name}/metrics/definitions`
  * `POST /api/agents/{name}/metrics/definitions/refresh`

Both gate on `AuthorizedAgentByName`, which is the **uniform-404** dependency
(Invariant #8): a non-existent agent and an inaccessible one must be
indistinguishable, or any caller can enumerate which agents exist. That is
asserted against the real dependency here rather than against the handlers,
because the handlers never branch on existence at all — which is exactly the
property under test.

Refresh is a **use**, not a **grant**: it re-reads the caller's own accessible
agent's file and can reach no other agent, so a shared user (who can already
`pull`) and the agent's own scoped key both pass. The stopped-agent case is a
named 409, never a silent throwaway-container spawn.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, seed_agent, seed_user  # noqa: E402,F401

pytest.importorskip("docker", reason="backend venv required")

from fastapi import HTTPException  # noqa: E402
import dependencies  # noqa: E402
from database import db  # noqa: E402
from routers import agent_files  # noqa: E402
from services import metric_registry  # noqa: E402

AGENT = "definitions-agent"
OWNER = "owner"

_TEMPLATE = """
name: demo
description: d
metrics:
  - name: cycles
    type: counter
    label: Cycles
    cadence: 1h
  - name: mood
    type: status
    values:
      - value: ok
        color: green
"""


def _request():
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"), headers={},
        url=SimpleNamespace(path="/"), state=SimpleNamespace(), method="GET",
    )


def _async_return(value):
    async def _f(*args, **kwargs):
        return value
    return _f


def _async_raise(exc):
    async def _f(*args, **kwargs):
        raise exc
    return _f


def _get(include_retired=False, agent=AGENT):
    return asyncio.run(agent_files.get_agent_metric_definitions(
        agent, _request(), include_retired=include_retired))


def _refresh(agent=AGENT):
    return asyncio.run(agent_files.refresh_agent_metric_definitions(
        agent, _request()))


def _declare(agent=AGENT, source="create", block=None):
    return metric_registry.reconcile_metric_definitions(
        agent,
        {"metrics": block if block is not None else [
            {"name": "cycles", "type": "counter", "label": "Cycles"}]},
        source=source,
    )


# ---------------------------------------------------------------------------
# The read
# ---------------------------------------------------------------------------

def test_declared_metrics_are_returned_with_their_fields(db_backend, monkeypatch):
    monkeypatch.setattr(metric_registry, "_read_template_from_container",
                        _async_return(_TEMPLATE))
    asyncio.run(metric_registry.refresh_from_running_agent(AGENT, source="refresh"))

    body = _get()
    assert body["agent_name"] == AGENT
    assert body["declared"] is True
    assert body["message"] is None
    names = [d["name"] for d in body["definitions"]]
    assert names == ["cycles", "mood"]
    cycles = body["definitions"][0]
    assert cycles["type"] == "counter"
    assert cycles["cadence_seconds"] == 3600
    assert cycles["label"] == "Cycles"
    assert cycles["status"] == "active"


def test_the_empty_state_teaches_the_next_action(db_backend):
    """Bar 3. A bare `[]` cannot tell an operator whether the agent declares
    nothing or whether the registry was never reconciled."""
    body = _get()
    assert body["definitions"] == []
    assert body["declared"] is False
    assert "no metrics: block in template.yaml" in body["message"]
    assert "refresh" in body["message"]


def test_retired_definitions_are_served_only_on_request(db_backend):
    _declare()
    _declare(source="pull", block=[])          # the template dropped its block

    assert _get()["definitions"] == []
    assert _get()["declared"] is False
    retired = _get(include_retired=True)["definitions"]
    assert [d["name"] for d in retired] == ["cycles"]
    assert retired[0]["status"] == "retired"


def test_an_agent_whose_every_metric_retired_is_not_declared(db_backend):
    """`declared` keys on ACTIVE rows: an agent with only retired definitions
    declares nothing today, whatever history it carries."""
    _declare()
    _declare(source="pull", block=[])
    assert _get(include_retired=True)["declared"] is False


def test_a_refused_type_change_is_visible_on_the_read(db_backend):
    """D-009 is a pure static check with no DB read, so it CANNOT name a type
    conflict. The definitions read and the refresh summary are the only two
    places an author can learn the platform is refusing their declaration —
    honest status (Bar 4)."""
    _declare()
    _declare(source="pull",
             block=[{"name": "cycles", "type": "gauge", "label": "Cycles"}])

    (row,) = _get()["definitions"]
    assert row["type"] == "counter"
    assert row["type_conflict"] == "gauge"


def test_the_response_carries_the_retention_contract(db_backend):
    """T2 — the names and defaults are published so a consumer reads them from
    the platform rather than hard-coding them.

    `enforced` FLIPPED to True with ent#478 (a deliberate red, named in that
    commit): the sweep, the cap and the two Settings knobs now exist, so the
    honest answer changed. The numbers are read live from the settings chain,
    which is why they are asserted against the resolver's own values rather
    than restated literals — see `test_ent478_settings_knobs.py` for the
    source-per-knob and the live-follow assertions."""
    policy = _get()["policy"]
    assert policy["retention_days"] == 365
    assert policy["daily_point_cap"] == 100000
    assert policy["enforced"] is True
    assert "478" in policy["enforced_by"]


def test_one_agents_read_never_shows_anothers(db_backend):
    _declare(agent="agent-a")
    _declare(agent="agent-b", block=[{"name": "other", "type": "gauge"}])
    assert [d["name"] for d in _get(agent="agent-a")["definitions"]] == ["cycles"]


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

def test_refresh_reconciles_and_returns_its_summary(db_backend, monkeypatch):
    monkeypatch.setattr(metric_registry, "_read_template_from_container",
                        _async_return(_TEMPLATE))
    body = _refresh()

    assert body["success"] is True
    assert body["source"] == "refresh"
    assert sorted(body["created"]) == ["cycles", "mood"]
    assert body["declared"] == 2
    assert [d["name"] for d in _get()["definitions"]] == ["cycles", "mood"]


def test_refresh_is_idempotent(db_backend, monkeypatch):
    """Invariant #18 does not apply (no execution is created) precisely because
    the operation is idempotent by construction — prove it."""
    monkeypatch.setattr(metric_registry, "_read_template_from_container",
                        _async_return(_TEMPLATE))
    _refresh()
    second = _refresh()
    assert second["created"] == [] and second["updated"] == []
    assert second["unchanged"] == 2


def test_refresh_on_a_stopped_agent_is_a_named_409(db_backend, monkeypatch):
    """Not a 404 (the agent exists and the caller can reach it) and not a 500.
    The stopped-agent read path spawns a THROWAWAY CONTAINER, and no
    request-triggered route may create a container as a side effect of a read."""
    monkeypatch.setattr(
        "services.docker_service.get_agent_container",
        lambda name: SimpleNamespace(status="exited"))

    with pytest.raises(HTTPException) as exc:
        _refresh()
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Refresh-Unavailable"] == "agent_not_running"


def test_refresh_never_spawns_a_container_for_a_missing_agent(
        db_backend, monkeypatch):
    monkeypatch.setattr(
        "services.docker_service.get_agent_container", lambda name: None)
    with pytest.raises(HTTPException) as exc:
        _refresh()
    assert exc.value.status_code == 409


def test_refresh_on_an_unreadable_template_is_a_named_503(db_backend, monkeypatch):
    monkeypatch.setattr(metric_registry, "_read_template_from_container",
                        _async_return("{{{ not yaml"))
    _declare()

    with pytest.raises(HTTPException) as exc:
        _refresh()
    assert exc.value.status_code == 503
    assert exc.value.headers["X-Refresh-Unavailable"] == "template_unreadable"
    # #2196 — the registry is untouched, never retired.
    assert _get()["definitions"][0]["status"] == "active"


def test_a_template_that_is_not_a_mapping_is_a_named_503(db_backend, monkeypatch):
    monkeypatch.setattr(metric_registry, "_read_template_from_container",
                        _async_return("- just\n- a list\n"))
    with pytest.raises(HTTPException) as exc:
        _refresh()
    assert exc.value.status_code == 503


def test_the_exec_is_a_fixed_argv_with_no_caller_input(db_backend, monkeypatch):
    """3.3 — no shell interpolation of anything a caller supplies, and the
    output is capped INSIDE the container so an oversized file never crosses
    the socket."""
    seen = {}

    async def _exec(container_name, command, timeout=None, **kwargs):
        seen["container"] = container_name
        seen["command"] = command
        return {"exit_code": 0, "output": _TEMPLATE}

    monkeypatch.setattr("services.docker_service.execute_command_in_container", _exec)
    monkeypatch.setattr(
        "services.docker_service.get_agent_container",
        lambda name: SimpleNamespace(status="running"))

    _refresh()
    assert seen["container"] == f"agent-{AGENT}"
    assert metric_registry.TEMPLATE_PATH in seen["command"]
    assert seen["command"].startswith("timeout ")
    assert "head -c" in seen["command"]
    assert ";" not in seen["command"] and "|" not in seen["command"]


@pytest.mark.parametrize("exec_result", [
    {"exit_code": 1, "output": "cat: no such file"},
    {"exit_code": 0, "output": ""},
    {"exit_code": 0, "output": "   \n"},
])
def test_a_failed_or_empty_exec_never_retires(db_backend, monkeypatch, exec_result):
    """Every one of these is "no evidence" — and retiring on any of them wipes
    a live registry on a transient fault."""
    _declare()
    monkeypatch.setattr(
        "services.docker_service.execute_command_in_container",
        _async_return(exec_result))
    monkeypatch.setattr(
        "services.docker_service.get_agent_container",
        lambda name: SimpleNamespace(status="running"))

    with pytest.raises(HTTPException) as exc:
        _refresh()
    assert exc.value.status_code == 503
    assert _get()["definitions"][0]["status"] == "active"


def test_an_oversized_template_is_refused_before_it_is_parsed(
        db_backend, monkeypatch):
    oversized = "x" * (metric_registry.MAX_TEMPLATE_BYTES + 100)
    monkeypatch.setattr(
        "services.docker_service.execute_command_in_container",
        _async_return({"exit_code": 0, "output": oversized}))
    monkeypatch.setattr(
        "services.docker_service.get_agent_container",
        lambda name: SimpleNamespace(status="running"))

    with pytest.raises(HTTPException) as exc:
        _refresh()
    assert exc.value.status_code == 503


# ---------------------------------------------------------------------------
# Auth — the dependency, not the handler (Invariant #8)
# ---------------------------------------------------------------------------

def _resolve(agent_name, user):
    """The dependency is sync today; tolerate either shape so a later refactor
    to `async def` does not turn this guard into a silent pass."""
    result = dependencies.get_authorized_agent_by_name(agent_name, user)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _user(username=OWNER, uid=1, **kwargs):
    base = dict(id=uid, username=username, email=f"{username}@example.com",
                agent_name=None, connector_agent=None, mcp_scope=None,
                role="user")
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_the_routes_gate_on_the_uniform_404_dependency():
    """Static, and deliberately so: the handlers never branch on existence, so
    there is no behaviour to test in them. The property is that the gate is the
    shared dependency — an inline `404`-then-`403` would be the enumeration
    oracle Invariant #8 forbids."""
    import inspect

    for handler in (agent_files.get_agent_metric_definitions,
                    agent_files.refresh_agent_metric_definitions):
        annotation = inspect.signature(handler).parameters["agent_name"].annotation
        assert annotation is dependencies.AuthorizedAgentByName, (
            f"{handler.__name__} must gate on AuthorizedAgentByName"
        )


def test_an_unknown_and_an_unowned_agent_are_indistinguishable(db_backend):
    """The enumeration property itself, through the real dependency."""
    seed_user(1, OWNER, "creator")
    seed_user(2, "stranger", "user")
    seed_agent(AGENT, owner_id=1)

    with pytest.raises(HTTPException) as unknown:
        _resolve("no-such-agent", _user("stranger", 2))
    with pytest.raises(HTTPException) as unowned:
        _resolve(AGENT, _user("stranger", 2))

    assert unknown.value.status_code == unowned.value.status_code == 404
    assert unknown.value.detail == unowned.value.detail


def test_the_owner_resolves(db_backend):
    seed_user(1, OWNER, "creator")
    seed_agent(AGENT, owner_id=1)
    assert _resolve(AGENT, _user()) == AGENT


def test_an_agent_scoped_key_reaches_its_own_agent(db_backend):
    """P11 — an agent must be able to refresh its own registry after editing
    its own template. A *use*, not a grant: the dependency resolves to the
    owner + access, so it can reach no other agent."""
    seed_user(1, OWNER, "creator")
    seed_agent(AGENT, owner_id=1)
    principal = _user(agent_name=AGENT, mcp_scope="agent")
    assert _resolve(AGENT, principal) == AGENT


def test_a_connector_principal_is_fenced_to_its_own_agent(db_backend):
    seed_user(1, OWNER, "creator")
    seed_agent(AGENT, owner_id=1)
    seed_agent("other-agent", owner_id=1)

    principal = _user(connector_agent="other-agent", mcp_scope="connector")
    with pytest.raises(HTTPException) as exc:
        _resolve(AGENT, principal)
    assert exc.value.status_code in (403, 404)


# ---------------------------------------------------------------------------
# Invariant #13 — the MCP surface is declared, not forgotten
# ---------------------------------------------------------------------------

def test_the_router_header_names_the_covering_mcp_tool():
    """T3 — no MCP tool ships here; ent#479's `get_metrics` covers the surface.
    `/validate-architecture` reads the first-line `# mcp:` header, so without
    this the two routes read as an oversight."""
    header = (_BACKEND / "routers" / "agent_files.py").read_text().splitlines()[0]
    assert header.startswith("# mcp:")
    assert "metrics/definitions" in header
    assert "ent#479" in header
