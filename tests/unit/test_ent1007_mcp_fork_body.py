"""trinity-enterprise#1007 (item 6) — the body MCP `create_agent` sends for a fork.

The MCP tool forwards `fork_to_own: {destination_repo, private?}` and
`import_intent: "fork"`, with no `github_pat` (MCP tool args are audit-logged).
`src/mcp-server/src/import-intent.test.ts` pins that the tool posts exactly
`MCP_FORK_BODY`; this file pins that the backend's `AgentConfig` keeps every key
of it — a key the receiving model silently drops would leave the MCP test green
while the fork never happens. It also pins that the REST contract did not move:
the create endpoint's `agents.manage` gate still runs first for a fork body.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

# Keep identical to the literal in src/mcp-server/src/import-intent.test.ts.
MCP_FORK_BODY = {
    "name": "a1",
    "template": "github:org/repo",
    "import_intent": "fork",
    "fork_to_own": {"destination_repo": "acme/sales-agent"},
}


def test_the_mcp_fork_body_survives_agent_config():
    from models import AgentConfig
    cfg = AgentConfig.model_validate(MCP_FORK_BODY)
    assert cfg.import_intent == "fork"
    assert cfg.fork_to_own is not None
    assert cfg.fork_to_own.destination_repo == "acme/sales-agent"
    # No PAT over MCP: the backend resolves the caller's saved token (#3164)
    # or, under an agent key, refuses FORK_PAT_REQUIRED.
    assert cfg.fork_to_own.github_pat is None
    assert cfg.fork_to_own.private is True


def test_private_false_is_carried():
    from models import AgentConfig
    body = {**MCP_FORK_BODY, "fork_to_own": {"destination_repo": "acme/x", "private": False}}
    assert AgentConfig.model_validate(body).fork_to_own.private is False


def test_a_fork_body_meets_the_create_gate_before_anything_is_created(monkeypatch):
    """The fork block does not open a path around `agents.manage`."""
    from database import db
    from models import AgentConfig
    from routers import agents as agents_router
    import importlib
    from unittest.mock import AsyncMock

    monkeypatch.setattr(db, "agent_has_capability", lambda agent, cap: False)
    monkeypatch.setattr(db, "get_agent_owner", lambda agent: {"owner_username": "owner"})
    audit = importlib.import_module("services.platform_audit_service")
    monkeypatch.setattr(audit.platform_audit_service, "log", AsyncMock(return_value="evt"))
    created = []

    async def fake_create(*a, **k):
        created.append(a)
        return {"name": "a1"}
    monkeypatch.setattr(agents_router, "create_agent_internal", fake_create)

    principal = SimpleNamespace(id=1, username="owner", email="o@example.com", role="admin",
                                mcp_scope="agent", agent_name="some-agent", connector_agent=None,
                                mcp_key_id="key-1")
    req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), method="POST",
                          scope={"path": "/api/agents"}, state=SimpleNamespace(request_id="r1"),
                          path_params={})
    with pytest.raises(HTTPException) as e:
        asyncio.run(agents_router.create_agent_endpoint(
            AgentConfig.model_validate(MCP_FORK_BODY), req, principal, None))
    assert e.value.detail["code"] == "agent_management_not_permitted"
    assert created == []
