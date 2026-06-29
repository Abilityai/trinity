"""Router-level guards for the MCP-exposure endpoints (#846).

The owner/unknown auth branches are enforced by the shared
``OwnedAgentByName`` / ``AuthorizedAgentByName`` dependencies (resolved before
the handler runs), so they're covered by the dependency's own tests. What lives
*inline* in the handlers — and is tested here by invoking the async functions
directly (no TestClient/auth stack) — is:

  * PUT refuses the system agent (403) before writing.
  * PUT success → db.set_mcp_exposed called + audit logged + tool_name returned.
  * GET returns {enabled, tool_name} from the shared slug helper.
  * the internal poll endpoint returns {agent_name, tool_name, description} per
    exposed agent, with the deterministic full-set slug.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)


def _load():
    try:
        from fastapi import HTTPException
        from models import McpExposedUpdate
        from routers import agents, internal
    except ImportError:
        pytest.skip("backend venv required")
    return HTTPException, McpExposedUpdate, agents, internal


def _user(uid=1):
    return SimpleNamespace(id=uid, agent_name=None, username="owner")


async def _noop_log(**kwargs):
    return None


# --------------------------------------------------------------------------
# PUT /mcp-exposed
# --------------------------------------------------------------------------


def test_put_refuses_system_agent(monkeypatch):
    HTTPException, McpExposedUpdate, agents, _ = _load()
    monkeypatch.setattr(agents.db, "is_system_agent", lambda name: True)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            agents.set_mcp_exposed_endpoint(
                agent_name="trinity-system",
                body=McpExposedUpdate(enabled=True),
                current_user=_user(),
            )
        )
    assert exc.value.status_code == 403


def test_put_success_sets_flag_and_audits(monkeypatch):
    HTTPException, McpExposedUpdate, agents, _ = _load()
    calls = {}
    monkeypatch.setattr(agents.db, "is_system_agent", lambda name: False)
    monkeypatch.setattr(agents.db, "set_mcp_exposed", lambda name, enabled: calls.setdefault("set", (name, enabled)) or True)
    monkeypatch.setattr(agents.platform_audit_service, "log", _noop_log)

    out = asyncio.run(
        agents.set_mcp_exposed_endpoint(
            agent_name="support-bot",
            body=McpExposedUpdate(enabled=True),
            current_user=_user(),
        )
    )
    assert calls["set"] == ("support-bot", True)
    assert out["agent_name"] == "support-bot"
    assert out["enabled"] is True
    assert out["tool_name"] == "chat_with_support_bot"


def test_get_returns_enabled_and_tool_name(monkeypatch):
    HTTPException, McpExposedUpdate, agents, _ = _load()
    monkeypatch.setattr(agents.db, "get_mcp_exposed", lambda name: True)
    out = asyncio.run(
        agents.get_mcp_exposed_endpoint(agent_name="Sales Desk", current_user=_user())
    )
    assert out["enabled"] is True
    assert out["tool_name"] == "chat_with_sales_desk"


# --------------------------------------------------------------------------
# GET /api/internal/mcp-exposed-agents
# --------------------------------------------------------------------------


def test_internal_endpoint_lists_tool_names_and_descriptions(monkeypatch):
    HTTPException, McpExposedUpdate, _, internal = _load()
    monkeypatch.setattr(
        internal.db,
        "get_mcp_exposed_agents",
        lambda: [{"agent_name": "support-bot"}, {"agent_name": "Sales Desk"}],
    )

    out = asyncio.run(internal.mcp_exposed_agents())
    by_name = {a["agent_name"]: a for a in out["agents"]}
    assert by_name["support-bot"]["tool_name"] == "chat_with_support_bot"
    assert by_name["Sales Desk"]["tool_name"] == "chat_with_sales_desk"
    # Descriptions are name-only by design (#846): advertised globally to every
    # non-connector MCP session, so they must NOT carry per-agent metadata. The
    # trinity.template label was a cross-tenant leak + injection surface and is
    # gone — this asserts the no-leak contract (regression guard).
    assert by_name["support-bot"]["description"] == 'Chat directly with the "support-bot" agent.'
    assert by_name["Sales Desk"]["description"] == 'Chat directly with the "Sales Desk" agent.'
    for agent in out["agents"]:
        assert "Template:" not in agent["description"]


def test_internal_endpoint_tolerates_docker_failure(monkeypatch):
    HTTPException, McpExposedUpdate, _, internal = _load()
    monkeypatch.setattr(
        internal.db, "get_mcp_exposed_agents", lambda: [{"agent_name": "a1"}]
    )
    import services.docker_service as docker_service

    def _boom():
        raise RuntimeError("docker down")

    monkeypatch.setattr(docker_service, "list_all_agents_fast", _boom)
    out = asyncio.run(internal.mcp_exposed_agents())
    # Falls back to a name-only description, never 5xx.
    assert out["agents"][0]["tool_name"] == "chat_with_a1"
    assert out["agents"][0]["description"] == 'Chat directly with the "a1" agent.'
