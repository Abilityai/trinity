"""FastAPI router for the per-agent MCP connector (ent#46; OSS-core since #118).

Exposes a single agent as a per-agent MCP connector: an end user adds it to their
AI client (Claude Code, Cursor, Claude Desktop) in one line, turning the agent's
``user_invocable`` playbooks into MCP tools. The agent holds all credentials
server-side; only a scoped, revocable key (``mcp_api_keys`` ``scope='connector'``)
reaches the client.

Agent-scoped endpoints under ``/api/agents/{name}/connector*`` (Invariant #15).
Owner-facing CRUD (config / mint-regenerate / revoke) uses ``OwnedAgentByName``.
The connector-key-readable ``/playbooks`` uses ``AuthorizedAgentByName`` — which
accepts a ``scope='connector'`` key fenced to its bound agent (the OSS auth fence
in ``dependencies.py``).

Relocated from the private ``enterprise/backend/mcp_connector/router.py`` when the
connector became OSS-core (#118): the ``requires_entitlement('mcp_connector')``
gate is removed and the router is mounted unconditionally in ``main.py``.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import List

from dependencies import (
    get_current_user,
    OwnedAgentByName,
    AuthorizedAgentByName,
    reject_agent_principal,
    _reject_connector_principal,
)
from models import (
    User,
    ConnectorConfigUpdate,
    ConnectorStatus,
    ConnectorKeySecret,
    ConnectorPlaybook,
)
from database import db
from services.docker_service import get_agent_container
from services.docker_utils import container_reload
from services.agent_auth import agent_httpx_client
from services.connector_service import build_snippets, resolve_exposed_playbooks
from routers.settings import resolve_mcp_url

router = APIRouter(prefix="/api/agents", tags=["mcp_connector"])

_KEY_PLACEHOLDER = "<CONNECTOR_KEY>"


def _status(agent_name: str, request: Request) -> ConnectorStatus:
    cfg = db.get_connector_config(agent_name)
    key_prefix = db.get_connector_key_prefix(agent_name)
    mcp_url = resolve_mcp_url(request)
    return ConnectorStatus(
        agent_name=agent_name,
        enabled=bool(cfg["enabled"]) if cfg else False,
        exposed_playbooks=cfg["exposed_playbooks"] if cfg else None,
        has_key=key_prefix is not None,
        key_prefix=key_prefix,
        mcp_url=mcp_url,
        snippets=build_snippets(agent_name, mcp_url, _KEY_PLACEHOLDER) if key_prefix else [],
        created_at=cfg["created_at"] if cfg else None,
        updated_at=cfg["updated_at"] if cfg else None,
    )


async def _fetch_live_playbooks(agent_name: str) -> List[dict]:
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")
    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=503, detail="Agent is not running.")
    try:
        url = f"http://agent-{agent_name}:8000/api/skills"
        async with agent_httpx_client(agent_name, timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json().get("skills", [])
            raise HTTPException(status_code=resp.status_code, detail=f"Agent error: {resp.text}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Agent is starting up, please try again")
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Could not connect to agent")


@router.get("/{agent_name}/connector", response_model=ConnectorStatus)
async def get_connector(agent_name: OwnedAgentByName, request: Request):
    return _status(agent_name, request)


@router.put("/{agent_name}/connector", response_model=ConnectorStatus)
async def update_connector(agent_name: OwnedAgentByName, body: ConnectorConfigUpdate, request: Request):
    db.upsert_connector_config(
        agent_name,
        enabled=body.enabled,
        exposed_playbooks=body.exposed_playbooks,
        clear_playbooks=bool(body.expose_all_playbooks),
    )
    return _status(agent_name, request)


@router.post("/{agent_name}/connector/key", response_model=ConnectorKeySecret)
async def regenerate_connector_key(
    agent_name: OwnedAgentByName,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Mint/regenerate the scoped key — returns the secret once; old key invalidated.

    Enabling the connector too, so a freshly-keyed connector is usable.

    Human-only (#1854): this is the one route in the file that returns a live
    key PLAINTEXT, and an agent-scoped principal resolves to the owner — so on
    an owner who owns several agents (and unconditionally for an admin) an agent
    could mint and read a sibling's connector credential. A connector key
    minting another connector key is likewise consumption-only breach.
    """
    reject_agent_principal(current_user)
    _reject_connector_principal(current_user)

    secret = db.regenerate_connector_key(agent_name, current_user.id)
    cfg = db.get_connector_config(agent_name)
    if not cfg or not cfg["enabled"]:
        db.upsert_connector_config(agent_name, enabled=True)
    mcp_url = resolve_mcp_url(request)
    return ConnectorKeySecret(
        agent_name=agent_name,
        api_key=secret["api_key"],
        key_prefix=secret["key_prefix"],
        mcp_url=mcp_url,
        snippets=build_snippets(agent_name, mcp_url, secret["api_key"]),
    )


@router.delete("/{agent_name}/connector/key")
async def revoke_connector_key(agent_name: OwnedAgentByName):
    db.revoke_connector_key(agent_name)
    return {"revoked": True, "agent_name": agent_name}


@router.get("/{agent_name}/connector/playbooks", response_model=List[ConnectorPlaybook])
async def list_connector_playbooks(agent_name: AuthorizedAgentByName):
    """Exposed playbooks (allow-list ∩ user_invocable) the connector advertises."""
    cfg = db.get_connector_config(agent_name)
    if cfg and not cfg["enabled"]:
        raise HTTPException(status_code=403, detail="Connector is disabled for this agent")
    live = await _fetch_live_playbooks(agent_name)
    allow = cfg["exposed_playbooks"] if cfg else None
    return resolve_exposed_playbooks(live, allow)
