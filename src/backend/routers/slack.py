"""
Slack integration router (SLACK-001).

Thin HTTP layer that delegates to the channel adapter abstraction.

Public Endpoints (no auth):
- POST /api/public/slack/events - Receive Slack webhook events (webhook mode only)
- GET /api/public/slack/oauth/callback - OAuth completion redirect

Authenticated Endpoints:
- GET /api/agents/{name}/public-links/{link_id}/slack - Connection status
- POST /api/agents/{name}/public-links/{link_id}/slack/connect - Start OAuth
- DELETE /api/agents/{name}/public-links/{link_id}/slack - Disconnect
- PUT /api/agents/{name}/public-links/{link_id}/slack - Update settings
"""

import logging
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import RedirectResponse

from database import db
from dependencies import get_current_user
from models import SlackChannelMessageRequest, SlackEventResponse, User
from services import rate_limiter
from services.slack_service import slack_service
from db_models import SlackConnectionStatus, SlackOAuthInitResponse
from services.settings_service import get_slack_signing_secret
from services.platform_audit_service import platform_audit_service, AuditEventType

logger = logging.getLogger(__name__)


# =========================================================================
# Transport reference — set by startup hook in main.py
# =========================================================================

_webhook_transport = None


def set_webhook_transport(transport):
    """Set the webhook transport instance (called from main.py startup)."""
    global _webhook_transport
    _webhook_transport = transport


# =========================================================================
# Public Router (no authentication required)
# =========================================================================

public_router = APIRouter(prefix="/api/public/slack", tags=["slack-public"])


@public_router.post("/events", response_model=SlackEventResponse)
async def handle_slack_event(request: Request):
    """
    Handle incoming Slack webhook events.

    Only active in webhook transport mode. Socket Mode bypasses this endpoint.
    Always returns 200 to prevent Slack retries.
    """
    if _webhook_transport:
        result = await _webhook_transport.handle_http_request(request)
        return SlackEventResponse(**result)

    # No webhook transport — maybe using Socket Mode, or not configured
    # Still handle URL verification challenges for initial setup
    try:
        body = await request.body()
        import json
        event_data = json.loads(body)
        if event_data.get("type") == "url_verification":
            return SlackEventResponse(ok=True, challenge=event_data.get("challenge"))
    except Exception:
        pass

    logger.warning("Slack webhook event received but no webhook transport configured")
    return SlackEventResponse(ok=False)


@public_router.get("/oauth/callback")
async def slack_oauth_callback(code: str = None, state: str = None, error: str = None):
    """
    Handle Slack OAuth callback.

    Exchanges code for token and creates connection.
    Redirects to frontend with success/error status.
    """
    # Handle OAuth errors
    if error:
        logger.error(f"Slack OAuth error: {error}")
        return RedirectResponse(
            slack_service.get_oauth_callback_redirect("unknown", success=False, error=error)
        )

    if not code or not state:
        return RedirectResponse(
            slack_service.get_oauth_callback_redirect("unknown", success=False, error="missing_params")
        )

    # Decode and verify state
    valid, state_data = slack_service.decode_oauth_state(state)
    if not valid or not state_data:
        return RedirectResponse(
            slack_service.get_oauth_callback_redirect("unknown", success=False, error="invalid_state")
        )

    link_id = state_data["link_id"]
    agent_name = state_data["agent_name"]
    user_id = state_data["user_id"]
    source = state_data.get("source", "agent")

    # Exchange code for token
    success, result = await slack_service.exchange_oauth_code(code)
    if not success:
        return RedirectResponse(
            slack_service.get_oauth_callback_redirect(agent_name, success=False, error=result.get("error"), source=source)
        )

    # Create workspace connection
    try:
        team_id = result["team_id"]
        team_name = result.get("team_name")
        bot_token = result["access_token"]

        # 1. Create/update workspace (new table)
        db.create_slack_workspace(
            team_id=team_id,
            team_name=team_name,
            bot_token=bot_token,
            connected_by=user_id
        )

        await platform_audit_service.log(
            event_type=AuditEventType.CREDENTIALS,
            event_action="oauth_complete",
            source="api",
            target_type="slack_workspace",
            target_id=team_id,
            endpoint="/oauth/callback",
            details={
                "provider": "slack",
                "team_name": team_name,
                "source": source,
                "agent_name": agent_name,
                "initiated_by_user_id": str(user_id) if user_id else None,
            },
        )

        # Platform install: just store workspace, no channel binding
        if source == "platform":
            logger.info(f"Slack workspace {team_name} installed via Settings")
            return RedirectResponse(
                slack_service.get_oauth_callback_redirect(agent_name, success=True, source="platform")
            )

        # Agent-level install: also create channel binding + legacy connection
        # 2. Create legacy connection (backward compat)
        try:
            db.create_slack_connection(
                link_id=link_id,
                slack_team_id=team_id,
                slack_team_name=team_name,
                slack_bot_token=bot_token,
                connected_by=user_id
            )
        except Exception as e:
            logger.debug(f"Legacy slack connection insert skipped: {e}")

        # 3. Create Slack channel for this agent and bind it
        try:
            success, channel_id, error = await slack_service.create_channel(bot_token, agent_name)
            if success and channel_id:
                db.bind_slack_channel_to_agent(
                    team_id=team_id,
                    slack_channel_id=channel_id,
                    slack_channel_name=agent_name,
                    agent_name=agent_name,
                    created_by=user_id,
                    is_dm_default=True,  # First agent is DM default
                )
                logger.info(f"Agent {agent_name} bound to Slack channel #{agent_name}")
            else:
                logger.warning(f"Failed to create Slack channel for {agent_name}: {error}")
        except Exception as e:
            logger.warning(f"Failed to create channel binding: {e}")

        logger.info(f"Slack connection created for workspace {team_name}")

        return RedirectResponse(
            slack_service.get_oauth_callback_redirect(agent_name, success=True)
        )

    except Exception as e:
        logger.error(f"Failed to create Slack connection: {e}")
        return RedirectResponse(
            slack_service.get_oauth_callback_redirect(agent_name, success=False, error="database_error", source=source)
        )


# =========================================================================
# Authenticated Router (requires login)
# =========================================================================

auth_router = APIRouter(tags=["slack"])


@auth_router.get("/api/agents/{name}/public-links/{link_id}/slack", response_model=SlackConnectionStatus)
async def get_slack_connection_status(
    name: str,
    link_id: str,
    current_user: User = Depends(get_current_user)
):
    """Get Slack connection status for a public link."""
    if not db.can_user_access_agent(current_user.username, name):
        raise HTTPException(status_code=403, detail="Access denied")

    link = db.get_public_link(link_id)
    if not link or link["agent_name"] != name:
        raise HTTPException(status_code=404, detail="Public link not found")

    connection = db.get_slack_connection_by_link(link_id)
    if not connection:
        return SlackConnectionStatus(connected=False)

    connected_by_user = db.get_user_by_id(int(connection["connected_by"])) if connection.get("connected_by") else None
    connected_by_email = connected_by_user.get("email", "unknown") if connected_by_user else "unknown"

    return SlackConnectionStatus(
        connected=True,
        team_id=connection["slack_team_id"],
        team_name=connection["slack_team_name"],
        connected_at=connection["connected_at"],
        connected_by=connected_by_email,
        enabled=connection["enabled"]
    )


@auth_router.post("/api/agents/{name}/public-links/{link_id}/slack/connect")
async def connect_slack(
    name: str,
    link_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Connect an agent to Slack.

    If workspace not yet connected: returns OAuth URL (frontend opens it).
    If workspace already connected: creates a Slack channel for the agent and binds it.
    """
    if not db.can_user_share_agent(current_user.username, name):
        raise HTTPException(status_code=403, detail="Only owners can connect Slack")

    link = db.get_public_link(link_id)
    if not link or link["agent_name"] != name:
        raise HTTPException(status_code=404, detail="Public link not found")

    # Check if workspace is already connected (any agent)
    # Look for existing workspace connection in new table
    existing_workspaces = []
    # Check legacy connections for any workspace
    all_connections = db.get_slack_connection_by_link(link_id)
    if all_connections:
        existing_workspaces.append(all_connections)

    # Find a connected workspace (bot token is decrypted automatically)
    workspaces = db.get_all_slack_workspaces()
    workspace = workspaces[0] if workspaces else None

    if workspace:
        # Workspace connected — create channel and bind agent
        try:
            # Create Slack channel named after the agent
            success, channel_id, error = await slack_service.create_channel(
                workspace["bot_token"], name
            )
            if not success:
                raise HTTPException(status_code=400, detail=f"Failed to create Slack channel: {error}")

            # Bind channel to agent
            agents_in_workspace = db.get_slack_agents_for_workspace(workspace["team_id"])
            is_first = len(agents_in_workspace) == 0

            db.bind_slack_channel_to_agent(
                team_id=workspace["team_id"],
                slack_channel_id=channel_id,
                slack_channel_name=name,
                agent_name=name,
                created_by=str(current_user.id),
                is_dm_default=is_first,  # First agent is DM default
            )

            # Also create legacy connection for backward compat
            try:
                db.create_slack_connection(
                    link_id=link_id,
                    slack_team_id=workspace["team_id"],
                    slack_team_name=workspace.get("team_name"),
                    slack_bot_token=workspace["bot_token"],
                    connected_by=str(current_user.id)
                )
            except Exception:
                pass  # May fail on UNIQUE — that's ok

            logger.info(f"Agent {name} bound to Slack channel #{name} in workspace {workspace.get('team_name')}")

            return {
                "status": "connected",
                "channel_id": channel_id,
                "channel_name": name,
                "workspace": workspace.get("team_name"),
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to bind agent to Slack channel: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    else:
        # No workspace connected — need OAuth first
        if not get_slack_signing_secret():
            raise HTTPException(status_code=400, detail="Slack integration not configured")

        state = slack_service.encode_oauth_state(link_id, name, str(current_user.id))

        try:
            oauth_url = slack_service.get_oauth_url(state)
            return {"status": "oauth_required", "oauth_url": oauth_url}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


@auth_router.delete("/api/agents/{name}/public-links/{link_id}/slack")
async def disconnect_slack(
    name: str,
    link_id: str,
    current_user: User = Depends(get_current_user)
):
    """Disconnect Slack workspace from public link."""
    if not db.can_user_share_agent(current_user.username, name):
        raise HTTPException(status_code=403, detail="Only owners can disconnect Slack")

    link = db.get_public_link(link_id)
    if not link or link["agent_name"] != name:
        raise HTTPException(status_code=404, detail="Public link not found")

    deleted = db.delete_slack_connection_by_link(link_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="No Slack connection found")

    logger.info(f"Slack disconnected from link {link_id} by user {current_user.id}")
    return {"disconnected": True}


@auth_router.put("/api/agents/{name}/public-links/{link_id}/slack")
async def update_slack_connection(
    name: str,
    link_id: str,
    enabled: bool = None,
    current_user: User = Depends(get_current_user)
):
    """Update Slack connection settings (enable/disable)."""
    if not db.can_user_share_agent(current_user.username, name):
        raise HTTPException(status_code=403, detail="Only owners can modify Slack settings")

    link = db.get_public_link(link_id)
    if not link or link["agent_name"] != name:
        raise HTTPException(status_code=404, detail="Public link not found")

    connection = db.get_slack_connection_by_link(link_id)
    if not connection:
        raise HTTPException(status_code=404, detail="No Slack connection found")

    db.update_slack_connection(connection["id"], enabled=enabled)
    return {"updated": True, "enabled": enabled}


# =========================================================================
# Per-Agent Slack Channel Binding (SLACK-002)
# =========================================================================


@auth_router.get("/api/agents/{name}/slack/channel")
async def get_agent_slack_channel(
    name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get the Slack channel binding for an agent.

    Returns binding info if the agent is bound to a channel,
    or {bound: false} if not.
    """
    if not db.can_user_access_agent(current_user.username, name):
        raise HTTPException(status_code=403, detail="Access denied")

    workspaces = db.get_all_slack_workspaces()
    for ws in workspaces:
        binding = db.get_slack_channel_for_agent(ws["team_id"], name)
        if binding:
            # Count agents in the workspace so the UI can decide whether
            # to allow unbinding — the DM-default agent cannot be unbound
            # while other agents are still bound (#584).
            workspace_agents = db.get_slack_agents_for_workspace(ws["team_id"])
            return {
                "bound": True,
                "channel_name": binding["slack_channel_name"],
                "channel_id": binding["slack_channel_id"],
                "workspace_team_id": ws["team_id"],
                "workspace_name": ws["team_name"],
                "is_dm_default": binding.get("is_dm_default", False),
                "workspace_agent_count": len(workspace_agents),
                "created_at": binding.get("created_at"),
            }

    return {"bound": False}


@auth_router.post("/api/agents/{name}/slack/channel")
async def create_agent_slack_channel(
    name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Create a Slack channel for an agent and bind it.

    Requires at least one connected workspace. Creates a channel
    named after the agent and binds it for message routing.
    """
    if not db.can_user_share_agent(current_user.username, name):
        raise HTTPException(status_code=403, detail="Only owners can manage Slack channels")

    # Check if already bound
    workspaces = db.get_all_slack_workspaces()
    if not workspaces:
        raise HTTPException(
            status_code=400,
            detail="No Slack workspace connected. Install a workspace from Settings first."
        )

    workspace = workspaces[0]

    # Check if agent already has a binding in this workspace
    existing = db.get_slack_channel_for_agent(workspace["team_id"], name)
    if existing:
        return {
            "status": "already_bound",
            "channel_name": existing["slack_channel_name"],
            "channel_id": existing["slack_channel_id"],
            "workspace_name": workspace["team_name"],
        }

    # Create channel in Slack
    bot_token = workspace["bot_token"]
    if not bot_token:
        raise HTTPException(status_code=500, detail="Workspace bot token is missing or could not be decrypted")

    success, channel_id, error = await slack_service.create_channel(bot_token, name)
    if not success:
        raise HTTPException(status_code=400, detail=f"Failed to create Slack channel: {error}")

    # Bind channel to agent
    agents_in_workspace = db.get_slack_agents_for_workspace(workspace["team_id"])
    is_first = len(agents_in_workspace) == 0

    db.bind_slack_channel_to_agent(
        team_id=workspace["team_id"],
        slack_channel_id=channel_id,
        slack_channel_name=name,
        agent_name=name,
        created_by=str(current_user.id),
        is_dm_default=is_first,
    )

    logger.info(f"Agent {name} bound to Slack channel #{name} in workspace {workspace['team_name']}")

    return {
        "status": "created",
        "channel_name": name,
        "channel_id": channel_id,
        "workspace_name": workspace["team_name"],
        "is_dm_default": is_first,
    }


@auth_router.delete("/api/agents/{name}/slack/channel")
async def delete_agent_slack_channel(
    name: str,
    current_user: User = Depends(get_current_user)
):
    """Unbind an agent from its Slack channel.

    Refuses to unbind the workspace's current DM-default agent while any
    other agents are still bound (#584). The owner must promote a different
    agent first via ``PUT /api/agents/{name}/slack/channel/dm-default``.
    When the agent is the only one bound, unbind is allowed — the workspace
    ends up with no Slack agents, which is a clean cascade.
    """
    if not db.can_user_share_agent(current_user.username, name):
        raise HTTPException(status_code=403, detail="Only owners can manage Slack channels")

    workspaces = db.get_all_slack_workspaces()
    for ws in workspaces:
        binding = db.get_slack_channel_for_agent(ws["team_id"], name)
        if not binding:
            continue

        # Refuse to drop the DM default while siblings remain.
        if binding.get("is_dm_default"):
            workspace_agents = db.get_slack_agents_for_workspace(ws["team_id"])
            if len(workspace_agents) > 1:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Cannot unbind the DM-default agent while other agents "
                        "are bound to this workspace. Set another agent as DM "
                        "default first (PUT /api/agents/{other}/slack/channel/"
                        "dm-default) and try again."
                    ),
                )

        if db.unbind_slack_agent(ws["team_id"], name):
            logger.info(f"Agent {name} unbound from Slack in workspace {ws['team_name']}")
            return {"unbound": True, "workspace_name": ws["team_name"]}

    raise HTTPException(status_code=404, detail="Agent is not bound to any Slack channel")


@auth_router.put("/api/agents/{name}/slack/channel/dm-default")
async def set_agent_as_slack_dm_default(
    name: str,
    current_user: User = Depends(get_current_user)
):
    """Make this agent the DM-default for its Slack workspace.

    DMs to the bot (no channel context, no @mention) route to whichever
    agent in the workspace is flagged ``is_dm_default=1``. Until #584 the
    flag was only auto-set for the first agent ever connected and had no
    setter, so workspaces with multiple agents were stuck. This endpoint
    flips it; ``unbind`` auto-promotes the oldest remaining agent so the
    workspace is never left with zero defaults.
    """
    if not db.can_user_share_agent(current_user.username, name):
        raise HTTPException(status_code=403, detail="Only owners can manage Slack channels")

    # Find the workspace where this agent is bound. There should be at
    # most one — agents are bound 1:1 per workspace today.
    workspace = None
    for ws in db.get_all_slack_workspaces():
        if db.get_slack_channel_for_agent(ws["team_id"], name):
            workspace = ws
            break

    if not workspace:
        raise HTTPException(
            status_code=404,
            detail="Agent is not bound to any Slack channel",
        )

    team_id = workspace["team_id"]
    previous = db.get_slack_dm_default_agent(team_id)
    if previous == name:
        # Idempotent — already the default.
        return {
            "status": "unchanged",
            "team_id": team_id,
            "workspace_name": workspace.get("team_name"),
            "previous": previous,
            "new_default": name,
        }

    if not db.set_slack_dm_default(team_id, name):
        # set_dm_default returns False only if the agent isn't bound — we
        # already verified that above, so this is a real "row vanished"
        # race. Surface as 404.
        raise HTTPException(status_code=404, detail="Agent binding not found")

    logger.info(
        "Slack DM default for workspace %s changed: %s → %s (by %s)",
        workspace.get("team_name"), previous, name, current_user.username,
    )

    # Audit
    try:
        await platform_audit_service.log(
            event_type=AuditEventType.AGENT_LIFECYCLE,
            event_action="slack_dm_default_changed",
            source="api",
            actor_user=current_user,
            target_type="agent",
            target_id=name,
            details={
                "team_id": team_id,
                "workspace_name": workspace.get("team_name"),
                "previous": previous,
                "new_default": name,
            },
        )
    except Exception as e:  # pragma: no cover
        logger.warning("Failed to audit slack_dm_default_changed: %s", e)

    return {
        "status": "updated",
        "team_id": team_id,
        "workspace_name": workspace.get("team_name"),
        "previous": previous,
        "new_default": name,
    }


# =============================================================================
# Proactive channel messaging (#350 Phase 3) — parallels Telegram #349
# =============================================================================

# Mirror the Telegram proactive caps (10/hr/channel, 100/hr/agent) via the
# shared sliding-window limiter (#1023) rather than a bespoke bucket.
_SLACK_PROACTIVE_PER_CHANNEL = 10
_SLACK_PROACTIVE_PER_AGENT = 100
_SLACK_PROACTIVE_WINDOW = 3600
_SLACK_MESSAGE_MAX = 4000


@auth_router.get("/api/agents/{name}/slack/channels")
async def list_agent_slack_channels(
    name: str,
    current_user: User = Depends(get_current_user),
):
    """List the Slack channels an agent is bound to (#350).

    Backs the MCP ``list_channel_groups`` discovery tool. Any user with access
    to the agent may list; proactive send is owner-gated separately.
    """
    if not db.can_user_access_agent(current_user.username, name):
        raise HTTPException(status_code=403, detail="Access denied")

    # team_id → workspace name, resolved once for labelling.
    ws_names = {ws["team_id"]: ws.get("team_name") for ws in db.get_all_slack_workspaces()}
    channels = [
        {
            "channel_type": "slack",
            "channel_id": c["slack_channel_id"],
            "channel_name": c.get("slack_channel_name"),
            "team_id": c["team_id"],
            "workspace_name": ws_names.get(c["team_id"]),
            "is_dm_default": c.get("is_dm_default", False),
        }
        for c in db.get_slack_channels_for_agent(name)
    ]
    return {"channels": channels, "count": len(channels)}


@auth_router.post("/api/agents/{name}/slack/channels/{channel_id}/messages")
async def send_agent_slack_channel_message(
    name: str,
    channel_id: str,
    request: SlackChannelMessageRequest,
    current_user: User = Depends(get_current_user),
):
    """Send a proactive message to a Slack channel the agent is bound to (#350).

    Owner-gated. Rate limited (10/hr/channel, 100/hr/agent) to prevent spam.
    Posts via ``chat.postMessage`` with the agent's identity, optionally in a
    thread.
    """
    if not db.can_user_share_agent(current_user.username, name):
        raise HTTPException(status_code=403, detail="Only owners can send channel messages")

    text = (request.message or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(text) > _SLACK_MESSAGE_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Message exceeds {_SLACK_MESSAGE_MAX} character limit",
        )

    # Validate the channel is actually bound to this agent (also yields team_id).
    target = next(
        (c for c in db.get_slack_channels_for_agent(name) if c["slack_channel_id"] == channel_id),
        None,
    )
    if not target:
        raise HTTPException(status_code=404, detail="Channel not found or not bound to this agent")

    # Rate limit: per-channel then per-agent (fail-open inside the limiter).
    rate_limiter.enforce(
        f"slack_proactive:{name}:{channel_id}",
        _SLACK_PROACTIVE_PER_CHANNEL, _SLACK_PROACTIVE_WINDOW,
        detail="Too many messages to this channel.",
    )
    rate_limiter.enforce(
        f"slack_proactive:{name}",
        _SLACK_PROACTIVE_PER_AGENT, _SLACK_PROACTIVE_WINDOW,
        detail="Too many proactive messages from this agent.",
    )

    bot_token = db.get_slack_workspace_bot_token(target["team_id"])
    if not bot_token:
        raise HTTPException(status_code=500, detail="Failed to retrieve Slack bot token")

    ok, error = await slack_service.send_message(
        bot_token=bot_token,
        channel=channel_id,
        text=text,
        username=name,  # per-message agent identity (chat:write.customize)
        thread_ts=request.thread_ts,
    )
    if not ok:
        raise HTTPException(status_code=502, detail=f"Slack send failed: {error}")

    return {
        "sent": True,
        "channel_type": "slack",
        "channel_id": channel_id,
        "channel_name": target.get("slack_channel_name"),
        "thread_ts": request.thread_ts,
    }
