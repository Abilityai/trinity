"""
Per-agent MCP connector endpoints (ent#46).

Owner-facing CRUD for the connector channel: enable/configure which playbooks
are exposed, mint/regenerate/revoke the scoped connector key, and read
copy-paste-ready per-client install snippets. Plus a connector-facing
`/playbooks` read used by the MCP server to advertise the exposed tools.

Three-layer: this router holds no SQL (db facade) and no snippet/format logic
(services/connector_service.py).
"""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import List

from models import (
    User,
    ConnectorConfigUpdate,
    ConnectorStatus,
    ConnectorKeySecret,
    ConnectorPlaybook,
)
from database import db
from dependencies import get_current_user, OwnedAgentByName, AuthorizedAgentByName
from services.docker_service import get_agent_container
from services.docker_utils import container_reload
from services.agent_auth import agent_httpx_client
from services.connector_service import build_snippets, resolve_exposed_playbooks
from routers.settings import MCP_URL_SETTING_KEY, _get_default_mcp_url

router = APIRouter(prefix="/api/agents", tags=["connector"])

# Placeholder shown in the status view's snippets (the real secret is only
# returned once, at mint/regenerate time).
_KEY_PLACEHOLDER = "<CONNECTOR_KEY>"


def _resolve_mcp_url(request: Request) -> str:
    """Configured external MCP URL, else the auto-detected default."""
    return db.get_setting_value(MCP_URL_SETTING_KEY) or _get_default_mcp_url(request)


def _status(agent_name: str, request: Request) -> ConnectorStatus:
    cfg = db.get_connector_config(agent_name)
    key = db.get_connector_mcp_api_key(agent_name)
    mcp_url = _resolve_mcp_url(request)
    return ConnectorStatus(
        agent_name=agent_name,
        enabled=bool(cfg.enabled) if cfg else False,
        exposed_playbooks=cfg.exposed_playbooks if cfg else None,
        has_key=key is not None,
        key_prefix=key.key_prefix if key else None,
        mcp_url=mcp_url,
        # Structural preview only — placeholder, never the live secret.
        snippets=build_snippets(agent_name, mcp_url, _KEY_PLACEHOLDER) if key else [],
        created_at=cfg.created_at if cfg else None,
        updated_at=cfg.updated_at if cfg else None,
    )


async def _fetch_live_playbooks(agent_name: str) -> List[dict]:
    """Proxy the agent container's /api/skills (SKILL.md frontmatter list)."""
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
    """Connector status + config + masked key + structural install snippets."""
    return _status(agent_name, request)


@router.put("/{agent_name}/connector", response_model=ConnectorStatus)
async def update_connector(
    agent_name: OwnedAgentByName,
    body: ConnectorConfigUpdate,
    request: Request,
):
    """Enable/disable + set the exposed-playbook allow-list (owner-only)."""
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
    """Mint (or regenerate) the scoped connector key — returns the secret once.

    Regenerating invalidates any previously embedded snippet immediately.
    Enabling the connector here too, so a freshly-keyed connector is usable.
    """
    secret = db.regenerate_connector_mcp_api_key(agent_name, current_user.username)
    if not secret:
        raise HTTPException(status_code=500, detail="Failed to create connector key")
    # First key implies intent to use it — ensure a config row exists + enabled.
    cfg = db.get_connector_config(agent_name)
    if not cfg or not cfg.enabled:
        db.upsert_connector_config(agent_name, enabled=True)

    mcp_url = _resolve_mcp_url(request)
    return ConnectorKeySecret(
        agent_name=agent_name,
        api_key=secret.api_key,
        key_prefix=secret.key_prefix,
        mcp_url=mcp_url,
        snippets=build_snippets(agent_name, mcp_url, secret.api_key),
    )


@router.delete("/{agent_name}/connector/key")
async def revoke_connector_key(agent_name: OwnedAgentByName):
    """Revoke the connector key (idempotent). Leaked snippets stop working."""
    db.delete_connector_mcp_api_key(agent_name)
    return {"revoked": True, "agent_name": agent_name}


@router.get("/{agent_name}/connector/playbooks", response_model=List[ConnectorPlaybook])
async def list_connector_playbooks(agent_name: AuthorizedAgentByName):
    """Exposed playbooks (allow-list ∩ user_invocable) the connector advertises.

    Accepts the connector-scoped key (resolved to the bound agent) or the
    owner. Authoritative server-side enforcement of the allow-list + the
    `user_invocable:false` exclusion — the MCP client never sees a hidden one.
    """
    cfg = db.get_connector_config(agent_name)
    if cfg and not cfg.enabled:
        raise HTTPException(status_code=403, detail="Connector is disabled for this agent")
    live = await _fetch_live_playbooks(agent_name)
    allow = cfg.exposed_playbooks if cfg else None
    return resolve_exposed_playbooks(live, allow)
