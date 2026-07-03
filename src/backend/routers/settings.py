"""
System settings routes for the Trinity backend.

Provides endpoints for managing system-wide configuration like the Trinity prompt.
Admin-only access for modification, read access for all authenticated users.
"""
import logging
import os
import re
import httpx
from typing import List, Dict, Any
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException, Request

logger = logging.getLogger(__name__)

from models import (
    AgentDefaultAccessPolicyUpdate,
    AgentDefaultResourcesUpdate,
    AgentQuotaUpdate,
    ApiKeyTest,
    ApiKeyUpdate,
    GitHubTemplatesUpdate,
    MaxParallelTasksCeilingUpdate,
    McpUrlUpdate,
    OpsSettingsUpdate,
    SlackConnectRequest,
    SlackSettingsUpdate,
    User,
)
from database import db, SystemSetting, SystemSettingUpdate
from dependencies import get_current_user
from services.platform_audit_service import platform_audit_service, AuditEventType

# Import from settings_service (these are re-exported for backward compatibility)
from services.settings_service import (
    get_anthropic_api_key,
    get_github_pat,
    get_google_api_key,
    get_ops_setting,
    settings_service,
    OPS_SETTINGS_DEFAULTS,
    OPS_SETTINGS_DESCRIPTIONS,
    AGENT_QUOTA_DEFAULTS,
    AGENT_QUOTA_DESCRIPTIONS,
    AGENT_DEFAULT_CPU_KEY,
    AGENT_DEFAULT_MEMORY_KEY,
    AGENT_DEFAULT_CPU,
    AGENT_DEFAULT_MEMORY,
    AGENT_DEFAULT_REQUIRE_EMAIL_KEY,
    AGENT_DEFAULT_REQUIRE_EMAIL,
    get_agent_default_require_email,
    MAX_PARALLEL_TASKS_CEILING_KEY,
    MAX_PARALLEL_TASKS_CEILING_DEFAULT,
    MAX_PARALLEL_TASKS_CEILING_MIN,
    MAX_PARALLEL_TASKS_CEILING_MAX,
    get_max_parallel_tasks_ceiling,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ============================================================================
# API Keys Management - Helper Functions
# ============================================================================


# Note: get_anthropic_api_key and get_github_pat are now imported from
# services.settings_service for proper architecture (services shouldn't import from routers)


def mask_api_key(key: str) -> str:
    """Mask an API key for display, showing only last 4 characters."""
    if not key or len(key) < 8:
        return "****"
    return f"...{key[-4:]}"


# ============================================================================
# Ops Settings Configuration
# ============================================================================

# Note: OPS_SETTINGS_DEFAULTS and OPS_SETTINGS_DESCRIPTIONS are now imported from
# services.settings_service for proper architecture


def require_admin(current_user: User):
    """Verify user is an admin."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("", response_model=List[SystemSetting])
async def get_all_settings(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get all system settings.

    Admin-only endpoint to view all configuration values.
    """
    require_admin(current_user)

    try:
        settings = db.get_all_settings()

        return settings
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get settings: {str(e)}")


@router.get("/feature-flags")
async def get_public_feature_flags(
    current_user: User = Depends(get_current_user)
):
    """
    Public-safe feature flags for the authenticated user.

    Unlike ``/api/settings/{key}`` (admin-only), this endpoint exposes a
    curated allowlist of UI-relevant flags so the frontend can decide
    which optional surfaces to render. Currently:

    - ``session_tab_enabled`` — gates the Session tab in AgentDetail.
      Reads through ``services.settings_service.is_session_tab_enabled()``
      so the resolution order (DB → env → False default) stays in one place.
    - ``workspace_available`` — gates the Agent Workspace (voice + canvas).
      Requires both voice infrastructure (VOICE_ENABLED + GEMINI_API_KEY) AND
      ``WORKSPACE_ENABLED=true`` (or DB override). Defaults to False (#860).

    Auth required (any role) — these flags reveal nothing sensitive but we
    still keep them out of the unauthenticated surface.
    """
    from config import (
        GEMINI_API_KEY,
        VOICE_ENABLED,
        VOIP_ENABLED,
        MCP_AGENT_CHAT_PULL_ENABLED,
        REDELIVERY_GOVERNOR_ENABLED,
    )
    from services.entitlement_service import entitlement_service
    voice_available = VOICE_ENABLED and bool(GEMINI_API_KEY)
    return {
        "session_tab_enabled": settings_service.is_session_tab_enabled(),
        "voice_available": voice_available,
        "workspace_available": voice_available and settings_service.is_workspace_enabled(),
        # VoIP telephony (VOIP-001, #1056) — default OFF, mirrors workspace_available.
        # Also requires a per-agent voip_bindings row to actually function.
        "voip_available": VOIP_ENABLED and bool(GEMINI_API_KEY),
        # Pull-pilot routing for agent→agent MCP chat (#946) — default OFF.
        # Observability-only here: the routing gate is the MCP server's own read
        # of the same env var. Lets an operator confirm, via the API, whether the
        # treatment window is active during the soak. NOT a UI surface.
        "mcp_agent_chat_pull_enabled": MCP_AGENT_CHAT_PULL_ENABLED,
        # Re-delivery governor (#1085) — default OFF. Observability-only here:
        # the actual gates live at the callback endpoint / reaper / drain read
        # points. Lets an operator confirm via the API whether the correlated-
        # failure controls are armed during a soak. NOT a UI surface.
        "redelivery_governor_enabled": REDELIVERY_GOVERNOR_ENABLED,
        "platform_default_model": settings_service.get_platform_default_model(),
        # Onboarding (trinity-enterprise#52) — is Claude auth configured at all?
        # Trinity agents can't think without it, so the first-run wizard uses
        # this to surface the one hard setup gate. True if a platform-wide
        # Anthropic key exists (DB or env) OR any Claude subscription is
        # registered. Non-sensitive: a boolean, never the key itself.
        "claude_auth_configured": bool(settings_service.get_anthropic_api_key())
        or db.has_any_subscription(),
        # #847 Phase 0 — enterprise entitlements. Empty list means OSS
        # build (or TRINITY_OSS_ONLY=1). UI uses this to hide
        # enterprise-only tabs cleanly without server-side conditional
        # rendering. Mirrors the deny-list pattern of the other flags.
        "enterprise_features": entitlement_service.list_entitled_features(),
    }


# ============================================================================
# API Keys Management Endpoints
# NOTE: These routes MUST be defined BEFORE the /{key} catch-all route
# ============================================================================

@router.get("/api-keys")
async def get_api_keys_status(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get status of configured API keys.

    Admin-only. Returns masked key info for security.
    """
    require_admin(current_user)

    try:
        # Get Anthropic key
        anthropic_key = get_anthropic_api_key()
        anthropic_configured = bool(anthropic_key)

        # Check if it's from settings or env
        key_from_settings = bool(db.get_setting_value('anthropic_api_key', None))

        # Get GitHub PAT
        github_pat = get_github_pat()
        github_configured = bool(github_pat)
        github_from_settings = bool(db.get_setting_value('github_pat', None))

        return {
            "anthropic": {
                "configured": anthropic_configured,
                "masked": mask_api_key(anthropic_key) if anthropic_configured else None,
                "source": "settings" if key_from_settings else ("env" if anthropic_configured else None)
            },
            "github": {
                "configured": github_configured,
                "masked": mask_api_key(github_pat) if github_configured else None,
                "source": "settings" if github_from_settings else ("env" if github_configured else None)
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get API keys status: {str(e)}")


@router.put("/api-keys/anthropic")
async def update_anthropic_key(
    body: ApiKeyUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set or update the Anthropic API key.

    Admin-only. Key is stored in system settings.
    """
    require_admin(current_user)

    try:
        # Validate format
        key = body.api_key.strip()
        if not key.startswith('sk-ant-'):
            raise HTTPException(
                status_code=400,
                detail="Invalid API key format. Anthropic keys start with 'sk-ant-'"
            )

        # Store in settings
        db.set_setting('anthropic_api_key', key)

        # SEC-001: audit API key change
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="settings_change",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"setting": "anthropic_api_key", "action": "update"},
        )

        return {
            "success": True,
            "masked": mask_api_key(key)
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update API key: {str(e)}")


@router.delete("/api-keys/anthropic")
async def delete_anthropic_key(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Delete the Anthropic API key from settings.

    Admin-only. Will fall back to env var if configured.
    """
    require_admin(current_user)

    try:
        deleted = db.delete_setting('anthropic_api_key')

        # SEC-001: audit API key deletion
        if deleted:
            await platform_audit_service.log(
                event_type=AuditEventType.CONFIGURATION,
                event_action="settings_change",
                source="api",
                actor_user=current_user,
                actor_ip=request.client.host if request.client else None,
                endpoint=str(request.url.path),
                request_id=getattr(request.state, "request_id", None),
                details={"setting": "anthropic_api_key", "action": "delete"},
            )

        # Check if env var fallback exists
        env_key = os.getenv('ANTHROPIC_API_KEY', '')

        return {
            "success": True,
            "deleted": deleted,
            "fallback_configured": bool(env_key)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete API key: {str(e)}")


@router.post("/api-keys/anthropic/test")
async def test_anthropic_key(
    body: ApiKeyTest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Test if an Anthropic API key is valid.

    Admin-only. Makes a lightweight API call to validate the key.
    """
    require_admin(current_user)

    try:
        key = body.api_key.strip()

        # Validate format first
        if not key.startswith('sk-ant-'):
            return {
                "valid": False,
                "error": "Invalid format. Anthropic keys start with 'sk-ant-'"
            }

        # Make a lightweight API call to test the key
        # Using the models endpoint which is simple and doesn't create any resources
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01"
                },
                timeout=10.0
            )

            if response.status_code == 200:
                return {"valid": True}
            elif response.status_code == 401:
                return {
                    "valid": False,
                    "error": "Invalid API key"
                }
            else:
                return {
                    "valid": False,
                    "error": f"API returned status {response.status_code}"
                }

    except httpx.TimeoutException:
        return {
            "valid": False,
            "error": "Request timed out. Please try again."
        }
    except Exception as e:
        return {
            "valid": False,
            "error": f"Error testing key: {str(e)}"
        }


@router.put("/api-keys/github")
async def update_github_pat(
    body: ApiKeyUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set or update the GitHub Personal Access Token.

    Admin-only. Token is stored in system settings and auto-propagated to all
    running agents that currently use the global PAT (#211).
    """
    require_admin(current_user)

    try:
        # Validate format
        key = body.api_key.strip()
        if not (key.startswith('ghp_') or key.startswith('github_pat_')):
            raise HTTPException(
                status_code=400,
                detail="Invalid token format. GitHub PATs start with 'ghp_' or 'github_pat_'"
            )

        # Store in settings
        db.set_setting('github_pat', key)

        # Auto-propagate to running agents (#211). Never block the PAT save on
        # propagation failures — the token is already persisted.
        from services.github_pat_propagation_service import propagate_github_pat
        try:
            propagation = await propagate_github_pat(key)
            propagation_payload: Dict[str, Any] = propagation.model_dump()
        except Exception as e:
            logger.exception("GitHub PAT propagation failed")
            propagation_payload = {"error": f"Propagation failed: {str(e)}"}

        # SEC-001: audit GitHub PAT change
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="settings_change",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"setting": "github_pat", "action": "update"},
        )

        return {
            "success": True,
            "masked": mask_api_key(key),
            "propagation": propagation_payload,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update GitHub PAT: {str(e)}")


@router.delete("/api-keys/github")
async def delete_github_pat(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Delete the GitHub PAT from settings.

    Admin-only. Will fall back to env var if configured.
    """
    require_admin(current_user)

    try:
        deleted = db.delete_setting('github_pat')

        # SEC-001: audit GitHub PAT deletion
        if deleted:
            await platform_audit_service.log(
                event_type=AuditEventType.CONFIGURATION,
                event_action="settings_change",
                source="api",
                actor_user=current_user,
                actor_ip=request.client.host if request.client else None,
                endpoint=str(request.url.path),
                request_id=getattr(request.state, "request_id", None),
                details={"setting": "github_pat", "action": "delete"},
            )

        # Check if env var fallback exists
        env_key = os.getenv('GITHUB_PAT', '')

        return {
            "success": True,
            "deleted": deleted,
            "fallback_configured": bool(env_key)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete GitHub PAT: {str(e)}")


@router.post("/api-keys/github/test")
async def test_github_pat(
    body: ApiKeyTest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Test if a GitHub PAT is valid.

    Admin-only. Makes a lightweight API call to validate the token.
    """
    require_admin(current_user)

    try:
        key = body.api_key.strip()

        # Validate format first
        if not (key.startswith('ghp_') or key.startswith('github_pat_')):
            return {
                "valid": False,
                "error": "Invalid format. GitHub PATs start with 'ghp_' or 'github_pat_'"
            }

        # Make a lightweight API call to test the token
        # Using the user endpoint which is simple and doesn't create any resources
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28"
                },
                timeout=10.0
            )

            if response.status_code == 200:
                data = response.json()

                # Determine token type and check permissions
                is_fine_grained = key.startswith('github_pat_')
                scopes = []
                has_repo_access = False

                if is_fine_grained:
                    # Fine-grained PATs: Test actual permissions by trying to list repos
                    # This will succeed if the token has proper permissions
                    try:
                        repos_response = await client.get(
                            "https://api.github.com/user/repos",
                            headers={
                                "Authorization": f"Bearer {key}",
                                "Accept": "application/vnd.github+json",
                                "X-GitHub-Api-Version": "2022-11-28"
                            },
                            params={"per_page": 1},  # Just test access, don't fetch all repos
                            timeout=10.0
                        )
                        # If we can list repos, the token has sufficient permissions
                        has_repo_access = repos_response.status_code == 200
                        scopes = ["fine-grained-pat"]
                    except Exception:
                        has_repo_access = False
                        scopes = ["fine-grained-pat"]
                else:
                    # Classic PAT: Check X-OAuth-Scopes header
                    scope_header = response.headers.get("X-OAuth-Scopes", "")
                    scopes = [s.strip() for s in scope_header.split(",") if s.strip()]
                    has_repo_access = "repo" in scopes or "public_repo" in scopes

                return {
                    "valid": True,
                    "username": data.get("login"),
                    "scopes": scopes,
                    "token_type": "fine-grained" if is_fine_grained else "classic",
                    "has_repo_access": has_repo_access
                }
            elif response.status_code == 401:
                return {
                    "valid": False,
                    "error": "Invalid Personal Access Token"
                }
            else:
                return {
                    "valid": False,
                    "error": f"GitHub API returned status {response.status_code}"
                }

    except httpx.TimeoutException:
        return {
            "valid": False,
            "error": "Request timed out. Please try again."
        }
    except Exception as e:
        return {
            "valid": False,
            "error": f"Error testing token: {str(e)}"
        }


# ============================================================================
# Slack Integration Settings (SLACK-001)
# ============================================================================

@router.get("/slack")
async def get_slack_settings_status(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get status of Slack integration settings.

    Admin-only. Returns masked key info for security.
    """
    require_admin(current_user)

    try:
        from services.settings_service import (
            get_slack_client_id,
            get_slack_client_secret,
            get_slack_signing_secret,
        )

        client_id = get_slack_client_id()
        client_secret = get_slack_client_secret()
        signing_secret = get_slack_signing_secret()

        # Check sources
        client_id_from_settings = bool(db.get_setting_value('slack_client_id', None))
        client_secret_from_settings = bool(db.get_setting_value('slack_client_secret', None))
        signing_secret_from_settings = bool(db.get_setting_value('slack_signing_secret', None))

        return {
            "configured": bool(client_id and client_secret and signing_secret),
            "client_id": {
                "configured": bool(client_id),
                "masked": mask_api_key(client_id) if client_id else None,
                "source": "settings" if client_id_from_settings else ("env" if client_id else None)
            },
            "client_secret": {
                "configured": bool(client_secret),
                "masked": mask_api_key(client_secret) if client_secret else None,
                "source": "settings" if client_secret_from_settings else ("env" if client_secret else None)
            },
            "signing_secret": {
                "configured": bool(signing_secret),
                "masked": mask_api_key(signing_secret) if signing_secret else None,
                "source": "settings" if signing_secret_from_settings else ("env" if signing_secret else None)
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get Slack settings: {str(e)}")


@router.put("/slack")
async def update_slack_settings(
    body: SlackSettingsUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Update Slack integration settings.

    Admin-only. All fields are optional - only provided values are updated.
    """
    require_admin(current_user)

    try:
        updated = []

        if body.client_id is not None:
            db.set_setting('slack_client_id', body.client_id.strip())
            updated.append('client_id')

        if body.client_secret is not None:
            db.set_setting('slack_client_secret', body.client_secret.strip())
            updated.append('client_secret')

        if body.signing_secret is not None:
            db.set_setting('slack_signing_secret', body.signing_secret.strip())
            updated.append('signing_secret')

        return {
            "success": True,
            "updated": updated
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update Slack settings: {str(e)}")


@router.delete("/slack")
async def delete_slack_settings(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Delete all Slack settings from database.

    Admin-only. Will fall back to env vars if configured.
    """
    require_admin(current_user)

    try:
        deleted = []
        for key in ['slack_client_id', 'slack_client_secret', 'slack_signing_secret']:
            if db.delete_setting(key):
                deleted.append(key)

        # Check env var fallbacks
        import os
        fallback_configured = bool(
            os.getenv('SLACK_CLIENT_ID') and
            os.getenv('SLACK_CLIENT_SECRET') and
            os.getenv('SLACK_SIGNING_SECRET')
        )

        return {
            "success": True,
            "deleted": deleted,
            "fallback_configured": fallback_configured
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete Slack settings: {str(e)}")


# ============================================================================
# Slack Transport Management (Socket Mode / Webhook)
# ============================================================================


@router.get("/slack/status")
async def get_slack_transport_status(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get Slack transport connection status.

    Returns transport mode, connection state, and workspace info.
    Admin-only.
    """
    require_admin(current_user)

    from services.settings_service import get_slack_app_token, get_slack_transport_mode

    transport = getattr(request.app.state, 'slack_transport', None)
    connected = transport is not None and transport.is_connected

    app_token = get_slack_app_token()
    transport_mode = get_slack_transport_mode()

    # Get workspace info
    workspaces_raw = db.get_all_slack_workspaces()
    workspaces = []
    for ws in workspaces_raw:
        agents = db.get_slack_agents_for_workspace(ws["team_id"])
        workspaces.append({
            "team_id": ws["team_id"],
            "team_name": ws["team_name"],
            "agent_count": len(agents),
            "agents": [a["agent_name"] for a in agents],
        })

    return {
        "connected": connected,
        "transport_mode": transport_mode,
        "app_token_configured": bool(app_token),
        "app_token_masked": mask_api_key(app_token) if app_token else None,
        "workspaces": workspaces,
    }


@router.post("/slack/connect")
async def connect_slack_transport(
    request: Request,
    body: SlackConnectRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Save Slack transport config and start the connection.

    Saves app_token and transport_mode to DB, stops any existing
    transport, and starts a new one. Admin-only.
    """
    require_admin(current_user)

    # Save settings to DB
    if body.app_token is not None:
        db.set_setting("slack_app_token", body.app_token.strip())
    if body.transport_mode is not None:
        if body.transport_mode.strip() not in ("socket", "webhook"):
            raise HTTPException(status_code=400, detail="transport_mode must be 'socket' or 'webhook'")
        db.set_setting("slack_transport_mode", body.transport_mode.strip())

    # Stop existing transport before starting new one
    existing = getattr(request.app.state, 'slack_transport', None)
    if existing:
        try:
            await existing.stop()
        except Exception as e:
            logger.warning(f"Error stopping existing Slack transport: {e}")
        request.app.state.slack_transport = None

    from services.settings_service import get_slack_app_token, get_slack_transport_mode, get_slack_signing_secret
    from adapters.slack_adapter import SlackAdapter
    from adapters.message_router import message_router

    mode = get_slack_transport_mode()
    adapter = SlackAdapter()
    transport = None

    try:
        if mode == "socket":
            app_token = get_slack_app_token()
            if not app_token:
                raise HTTPException(status_code=400, detail="App token required for Socket Mode")
            from adapters.transports.slack_socket import SlackSocketTransport
            transport = SlackSocketTransport(app_token, adapter, message_router)
            await transport.start()
            if not transport.is_connected:
                raise HTTPException(status_code=400, detail="Failed to connect Socket Mode. Check app token is valid.")
        else:
            signing_secret = get_slack_signing_secret()
            if not signing_secret:
                raise HTTPException(status_code=400, detail="Signing secret required for Webhook Mode")
            from adapters.transports.slack_webhook import SlackWebhookTransport
            transport = SlackWebhookTransport(signing_secret, adapter, message_router)
            await transport.start()
            # Register webhook transport for the events endpoint
            from routers.slack import set_webhook_transport
            set_webhook_transport(transport)

        request.app.state.slack_transport = transport

        return {
            "connected": True,
            "transport_mode": mode,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to start Slack transport: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to connect: {str(e)}")


@router.post("/slack/install")
async def install_slack_workspace(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Generate Slack OAuth URL to install the app to a workspace.

    Redirects user to Slack for authorization. After approval, Slack
    redirects back to the OAuth callback which stores the bot token
    and redirects to Settings page. Admin-only.
    """
    require_admin(current_user)

    from services.settings_service import get_slack_client_id
    from services.slack_service import slack_service

    if not get_slack_client_id():
        raise HTTPException(status_code=400, detail="Slack Client ID not configured. Save OAuth credentials first.")

    try:
        state = slack_service.encode_oauth_state(
            link_id="platform",
            agent_name="platform",
            user_id=str(current_user.id),
            source="platform"
        )
        oauth_url = slack_service.get_oauth_url(state)
        return {"oauth_url": oauth_url}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/slack/disconnect")
async def disconnect_slack_transport(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Stop the Slack transport. Does not delete saved credentials.

    Admin-only.
    """
    require_admin(current_user)

    transport = getattr(request.app.state, 'slack_transport', None)
    if not transport:
        return {"disconnected": True, "was_connected": False}

    try:
        await transport.stop()
    except Exception as e:
        logger.warning(f"Error stopping Slack transport: {e}")

    request.app.state.slack_transport = None

    return {"disconnected": True, "was_connected": True}


# ============================================================================
# Email Whitelist Management (Phase 12.4)
# ============================================================================

@router.get("/email-whitelist")
async def list_email_whitelist(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    List all whitelisted emails.

    Admin-only endpoint.
    """
    require_admin(current_user)

    whitelist = db.list_whitelist(limit=1000)

    return {"whitelist": whitelist}


@router.post("/email-whitelist")
async def add_email_to_whitelist(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Add an email to the whitelist.

    Admin-only endpoint.
    """
    from database import EmailWhitelistAdd

    require_admin(current_user)

    # Parse request
    body = await request.json()
    add_request = EmailWhitelistAdd(**body)
    email = add_request.email.lower()

    # Add to whitelist
    try:
        added = db.add_to_whitelist(
            email,
            current_user.username,
            source=add_request.source,
            default_role=add_request.default_role,
        )

        if not added:
            raise HTTPException(
                status_code=409,
                detail=f"Email {email} is already whitelisted"
            )

        return {"success": True, "email": email}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/email-whitelist/{email}")
async def remove_email_from_whitelist(
    email: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Remove an email from the whitelist.

    Admin-only endpoint.
    """
    require_admin(current_user)

    # Remove from whitelist
    removed = db.remove_from_whitelist(email)

    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"Email {email} not found in whitelist"
        )

    return {"success": True, "email": email}

# ============================================================================
# GitHub Templates Configuration (TMPL-001)
# ============================================================================


_REPO_PATTERN = re.compile(r'^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$')


@router.get("/github-templates")
async def get_github_templates(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get configured GitHub templates.

    Admin-only. Returns the configured list or the hardcoded defaults.
    Display names and descriptions are resolved from each repo's template.yaml.
    """
    require_admin(current_user)

    from config import DEFAULT_GITHUB_TEMPLATE_REPOS
    from services.template_service import _fetch_all_metadata

    db_templates = settings_service.get_github_templates()
    if db_templates is not None:
        repos = [e["github_repo"] for e in db_templates]
        all_metadata = _fetch_all_metadata(repos)
        enriched = []
        for entry in db_templates:
            repo = entry["github_repo"]
            meta = all_metadata.get(repo, {})
            admin_name = entry.get("display_name", "")
            admin_desc = entry.get("description", "")
            enriched.append({
                "github_repo": repo,
                "display_name": admin_name,
                "description": admin_desc,
                "resolved_name": admin_name or meta.get("display_name") or meta.get("name") or repo.split("/")[-1],
                "resolved_description": admin_desc or meta.get("description", ""),
            })
        return {
            "source": "settings",
            "templates": enriched
        }
    else:
        all_metadata = _fetch_all_metadata(DEFAULT_GITHUB_TEMPLATE_REPOS)
        defaults = []
        for repo in DEFAULT_GITHUB_TEMPLATE_REPOS:
            meta = all_metadata.get(repo, {})
            defaults.append({
                "github_repo": repo,
                "display_name": "",
                "description": "",
                "resolved_name": meta.get("display_name") or meta.get("name") or repo.split("/")[-1],
                "resolved_description": meta.get("description", ""),
            })
        return {
            "source": "defaults",
            "templates": defaults
        }


@router.put("/github-templates")
async def update_github_templates(
    body: GitHubTemplatesUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set the GitHub templates list.

    Admin-only. Validates owner/repo format for each entry.
    """
    require_admin(current_user)

    # Validate each entry
    for entry in body.templates:
        if not _REPO_PATTERN.match(entry.github_repo):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid repository format: '{entry.github_repo}'. Expected 'owner/repo'."
            )

    # Convert to list of dicts for storage
    templates_data = [entry.model_dump() for entry in body.templates]
    settings_service.set_github_templates(templates_data)

    return {
        "success": True,
        "count": len(templates_data)
    }


@router.delete("/github-templates")
async def delete_github_templates(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Reset GitHub templates to hardcoded defaults.

    Admin-only. Removes the DB override so the system falls back to config.py defaults.
    """
    require_admin(current_user)

    deleted = settings_service.delete_github_templates()

    return {
        "success": True,
        "deleted": deleted,
        "message": "GitHub templates reset to defaults"
    }


# ============================================================================
# MCP Server URL Configuration (#76)
# ============================================================================

MCP_URL_SETTING_KEY = "mcp_external_url"


def _get_default_mcp_url(request: Request) -> str:
    """Compute the auto-detected MCP URL from the request hostname."""
    host = request.headers.get("host", "localhost:8080")
    hostname = host.split(":")[0]
    if hostname in ("localhost", "127.0.0.1"):
        return "http://localhost:8080/mcp"
    return f"http://{hostname}:8080/mcp"


def resolve_mcp_url(request: Request) -> str:
    """Effective MCP URL: the operator-configured override, else auto-detected.

    Public accessor so other modules (e.g. the entitled MCP connector) don't
    reach into the private helper above.
    """
    return db.get_setting_value(MCP_URL_SETTING_KEY) or _get_default_mcp_url(request)


def _validate_mcp_url(url: str) -> str:
    """Validate and normalize MCP URL. Returns normalized URL or raises HTTPException."""
    url = url.strip().rstrip("/")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=422,
            detail="URL must start with http:// or https://"
        )
    if not parsed.netloc:
        raise HTTPException(
            status_code=422,
            detail="Invalid URL format"
        )
    if not parsed.path.endswith("/mcp"):
        raise HTTPException(
            status_code=422,
            detail="URL must end with /mcp"
        )

    return url


@router.get("/mcp-url")
async def get_mcp_url(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get the configured MCP server URL.

    Any authenticated user can read this (used by API Keys page).
    Returns both the stored custom URL (if any) and the auto-detected default.
    """
    stored_url = db.get_setting_value(MCP_URL_SETTING_KEY)
    default_url = _get_default_mcp_url(request)

    return {
        "url": stored_url,
        "default_url": default_url
    }


@router.put("/mcp-url")
async def update_mcp_url(
    body: McpUrlUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set a custom MCP server URL.

    Admin-only. Validates URL format (must be http/https, must end with /mcp).
    """
    require_admin(current_user)

    validated_url = _validate_mcp_url(body.url)
    db.set_setting(MCP_URL_SETTING_KEY, validated_url)

    return {
        "success": True,
        "url": validated_url
    }


@router.delete("/mcp-url")
async def delete_mcp_url(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Reset MCP server URL to auto-detect.

    Admin-only. Removes the custom URL, reverting to hostname-based auto-detection.
    """
    require_admin(current_user)

    deleted = db.delete_setting(MCP_URL_SETTING_KEY)

    return {
        "success": True,
        "deleted": deleted,
        "message": "MCP server URL reset to auto-detect"
    }


# ============================================================================
# Generic Settings CRUD - /{key} catch-all routes
# NOTE: These must come AFTER specific routes like /api-keys
# ============================================================================

# ============================================================================
# Agent Quota Settings (QUOTA-001)
# ============================================================================


@router.get("/agent-quotas")
async def get_agent_quotas(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get per-role agent quota configuration.

    Admin-only. Returns quota limits for each role with defaults.
    Admin role is always unlimited and not configurable.
    """
    require_admin(current_user)

    all_settings = db.get_settings_dict()

    # Check for legacy setting
    legacy_value = all_settings.get("max_agents_per_user")

    quotas = {}
    for key, default_value in AGENT_QUOTA_DEFAULTS.items():
        current_value = all_settings.get(key, default_value)
        quotas[key] = {
            "value": current_value,
            "default": default_value,
            "description": AGENT_QUOTA_DESCRIPTIONS.get(key, ""),
            "is_default": key not in all_settings
        }

    return {
        "quotas": quotas,
        "admin_unlimited": True,
        "legacy_setting": legacy_value
    }


@router.put("/agent-quotas")
async def update_agent_quotas(
    body: AgentQuotaUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Update per-role agent quotas.

    Admin-only. Only updates provided fields. Values must be non-negative integers.
    Set to "0" for unlimited.
    """
    require_admin(current_user)

    updated = []
    for key in AGENT_QUOTA_DEFAULTS:
        value = getattr(body, key, None)
        if value is not None:
            try:
                int_val = int(value)
                if int_val < 0:
                    raise HTTPException(status_code=400, detail=f"Quota value for {key} must be non-negative")
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Quota value for {key} must be an integer")
            db.set_setting(key, value)
            updated.append(key)

    return {
        "success": True,
        "updated": updated
    }


# ============================================================================
# Agent Default Resources (RES-001)
# ============================================================================

# Canonical allowed values live in the container-spec module so the admin
# defaults endpoint and the agent create/recreate paths can't drift (#1197).
from services.agent_service.capabilities import VALID_CPU, VALID_MEMORY
VALID_CPU_VALUES = list(VALID_CPU)
VALID_MEMORY_VALUES = list(VALID_MEMORY)


@router.get("/agent-defaults/resources")
async def get_agent_default_resources(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get the system-wide default CPU and memory limits applied to every new agent container.

    Admin-only.
    """
    require_admin(current_user)

    cpu = db.get_setting_value(AGENT_DEFAULT_CPU_KEY, AGENT_DEFAULT_CPU)
    memory = db.get_setting_value(AGENT_DEFAULT_MEMORY_KEY, AGENT_DEFAULT_MEMORY)

    return {
        "cpu": cpu,
        "memory": memory,
        "cpu_default": AGENT_DEFAULT_CPU,
        "memory_default": AGENT_DEFAULT_MEMORY,
        "valid_cpu_values": VALID_CPU_VALUES,
        "valid_memory_values": VALID_MEMORY_VALUES,
        "note": "Changes apply to new agent containers only. Restart existing agents to pick up new defaults."
    }


@router.put("/agent-defaults/resources")
async def update_agent_default_resources(
    body: AgentDefaultResourcesUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set the system-wide default CPU and memory limits for new agent containers.

    Admin-only. Only the fields provided are updated.
    Valid CPU values (number of processors): 1, 2, 4, 8, 16
    Valid memory values: 1g, 2g, 4g, 8g, 16g, 32g
    """
    require_admin(current_user)

    updated = []

    if body.cpu is not None:
        if body.cpu not in VALID_CPU_VALUES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid CPU value. Must be one of: {', '.join(VALID_CPU_VALUES)}"
            )
        db.set_setting(AGENT_DEFAULT_CPU_KEY, body.cpu)
        updated.append("cpu")

    if body.memory is not None:
        if body.memory not in VALID_MEMORY_VALUES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid memory value. Must be one of: {', '.join(VALID_MEMORY_VALUES)}"
            )
        db.set_setting(AGENT_DEFAULT_MEMORY_KEY, body.memory)
        updated.append("memory")

    cpu = db.get_setting_value(AGENT_DEFAULT_CPU_KEY, AGENT_DEFAULT_CPU)
    memory = db.get_setting_value(AGENT_DEFAULT_MEMORY_KEY, AGENT_DEFAULT_MEMORY)

    return {
        "success": True,
        "updated": updated,
        "cpu": cpu,
        "memory": memory,
        "restart_required": True
    }


# ============================================================================
# Agent Default Access Policy (#1129 — secure-by-default require_email)
# ============================================================================

@router.get("/agent-defaults/access-policy")
async def get_agent_default_access_policy(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get the fleet-wide default access policy applied to newly created agents.

    Currently scopes to `require_email` (#1129): when ON, new agents require a
    verified email on incoming DMs / public chat / shared access. Admin-only.
    """
    require_admin(current_user)

    return {
        "require_email": get_agent_default_require_email(),
        "require_email_default": AGENT_DEFAULT_REQUIRE_EMAIL,
        "note": "Applies to newly created agents only. Existing agents keep their "
                "current per-agent value; owners can override per agent via the "
                "agent's access policy.",
    }


@router.put("/agent-defaults/access-policy")
async def update_agent_default_access_policy(
    body: AgentDefaultAccessPolicyUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set the fleet-wide default access policy for new agents (#1129).

    Admin-only. Only the fields provided are updated. Stored in system_settings;
    consumed at agent-creation time. Does NOT rewrite existing agents.
    """
    require_admin(current_user)

    updated = []
    if body.require_email is not None:
        db.set_setting(AGENT_DEFAULT_REQUIRE_EMAIL_KEY, "1" if body.require_email else "0")
        updated.append("require_email")

        # SEC-001 / #1129: audit this security-relevant default change — flipping
        # the fleet-wide email-verification default weakens/strengthens the
        # posture for every future agent, so it must leave a trace (mirrors the
        # API-key / generic-setting audit path in this router).
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="settings_change",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={
                "setting": "agent_default_require_email",
                "action": "update",
                "require_email": body.require_email,
            },
        )

    return {
        "success": True,
        "updated": updated,
        "require_email": get_agent_default_require_email(),
    }


# ============================================================================
# Max Parallel Tasks Ceiling (#506 — fleet-wide per-agent concurrency cap)
# ============================================================================

@router.get("/max-parallel-tasks-ceiling")
async def get_max_parallel_tasks_ceiling_setting(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get the fleet-wide ceiling on any single agent's max_parallel_tasks (#506).

    Admin-only. Owners pick a per-agent value within this ceiling.
    Registered before the `/{key}` catch-all (Invariant #4).
    """
    require_admin(current_user)

    return {
        "value": get_max_parallel_tasks_ceiling(),
        "default": MAX_PARALLEL_TASKS_CEILING_DEFAULT,
        "min": MAX_PARALLEL_TASKS_CEILING_MIN,
        "max": MAX_PARALLEL_TASKS_CEILING_MAX,
    }


@router.put("/max-parallel-tasks-ceiling")
async def update_max_parallel_tasks_ceiling_setting(
    body: MaxParallelTasksCeilingUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set the fleet-wide ceiling on per-agent max_parallel_tasks (#506).

    Admin-only. Validates MIN ≤ value ≤ MAX (1–32). The clamp applies at
    runtime (CapacityManager facade + bypasses); stored per-agent values are
    never rewritten. Existing agents above the new ceiling are clamped on the
    next admit.
    """
    require_admin(current_user)

    if (
        not isinstance(body.value, int)
        or body.value < MAX_PARALLEL_TASKS_CEILING_MIN
        or body.value > MAX_PARALLEL_TASKS_CEILING_MAX
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"max_parallel_tasks_ceiling must be an integer between "
                f"{MAX_PARALLEL_TASKS_CEILING_MIN} and {MAX_PARALLEL_TASKS_CEILING_MAX}"
            ),
        )

    db.set_setting(MAX_PARALLEL_TASKS_CEILING_KEY, str(body.value))

    # SEC-001: audit this fleet-wide capacity change (mirrors the access-policy
    # default audit path) — it caps concurrency for every agent on the host.
    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="settings_change",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={
            "setting": MAX_PARALLEL_TASKS_CEILING_KEY,
            "action": "update",
            "value": body.value,
        },
    )

    return {
        "success": True,
        "value": get_max_parallel_tasks_ceiling(),
        "default": MAX_PARALLEL_TASKS_CEILING_DEFAULT,
        "min": MAX_PARALLEL_TASKS_CEILING_MIN,
        "max": MAX_PARALLEL_TASKS_CEILING_MAX,
    }


@router.get("/{key}")
async def get_setting(
    key: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get a specific setting by key.

    Returns the setting value or 404 if not found.
    Admin-only for most settings.
    """
    require_admin(current_user)

    try:
        setting = db.get_setting(key)

        if not setting:
            raise HTTPException(status_code=404, detail=f"Setting '{key}' not found")

        return setting
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get setting: {str(e)}")


@router.put("/{key}", response_model=SystemSetting)
async def update_setting(
    key: str,
    body: SystemSettingUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Create or update a system setting.

    Admin-only endpoint. Creates the setting if it doesn't exist.
    """
    require_admin(current_user)

    # #506: the fleet ceiling must go through the dedicated range-validated
    # route; block the generic PUT so it can't be written to junk/out-of-range
    # (same pattern as the skills_library_url SSRF special-case below).
    if key == MAX_PARALLEL_TASKS_CEILING_KEY:
        raise HTTPException(
            status_code=422,
            detail=(
                "max_parallel_tasks_ceiling must be set via "
                "PUT /api/settings/max-parallel-tasks-ceiling (range-validated 1–32)"
            ),
        )

    # Validate URL-based settings to prevent SSRF (SEC-179)
    if key == "skills_library_url" and body.value:
        from utils.url_validation import validate_skills_library_url
        try:
            validate_skills_library_url(body.value)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    try:
        setting = db.set_setting(key, body.value)

        # #831: Invalidate platform default model TTL cache on write
        if key == "platform_default_model":
            import services.settings_service as _ss
            _ss._platform_model_cache = None
            _ss._platform_model_cache_ts = 0.0

        # SEC-001: audit generic setting change
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="settings_change",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"setting": key, "action": "update"},
        )

        # Back-fill Telegram webhooks when public_chat_url becomes available.
        # Why: bindings created before public_chat_url was set have webhook_url IS NULL
        # and receive no messages. Re-registering is idempotent (setWebhook on Telegram).
        if key == "public_chat_url" and body.value:
            await _backfill_telegram_webhooks(body.value)
            # Same back-fill for WhatsApp — refreshes the URL shown to the user
            # for pasting into Twilio Console. (Twilio doesn't have a setWebhook
            # API equivalent — users paste the URL manually.)
            try:
                from adapters.transports.twilio_webhook import backfill_webhook_urls as _wa_backfill
                _wa_backfill(body.value)
            except Exception as e:
                logger.warning(f"WhatsApp webhook URL back-fill skipped: {e}")

        return setting
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update setting: {str(e)}")


async def _backfill_telegram_webhooks(public_url: str) -> None:
    """Re-register Telegram webhooks for all bindings after public_chat_url changes.

    Idempotent: Telegram's setWebhook replaces any existing registration.
    Failures are logged but not raised — the setting write has already succeeded
    and a single bad binding must not block others or the response.
    """
    try:
        from adapters.transports.telegram_webhook import register_webhook
        bindings = db.get_all_telegram_bindings()
    except Exception as e:
        logger.warning(f"Telegram webhook back-fill skipped: {e}")
        return

    for binding in bindings:
        agent_name = binding.get("agent_name", "<unknown>")
        try:
            await register_webhook(agent_name, public_url)
        except Exception as e:
            logger.warning(
                f"Telegram webhook back-fill failed for agent={agent_name}: {e}"
            )


@router.delete("/{key}")
async def delete_setting(
    key: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Delete a system setting.

    Admin-only endpoint. Returns success even if setting didn't exist.
    """
    require_admin(current_user)

    try:
        deleted = db.delete_setting(key)

        # SEC-001: audit setting deletion
        if deleted:
            await platform_audit_service.log(
                event_type=AuditEventType.CONFIGURATION,
                event_action="settings_change",
                source="api",
                actor_user=current_user,
                actor_ip=request.client.host if request.client else None,
                endpoint=str(request.url.path),
                request_id=getattr(request.state, "request_id", None),
                details={"setting": key, "action": "delete"},
            )

        return {"success": True, "deleted": deleted}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete setting: {str(e)}")


# ============================================================================
# Ops Settings Endpoints
# ============================================================================

@router.get("/ops/config")
async def get_ops_settings(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get all ops-related settings with their current values and defaults.

    Admin-only. Returns both stored values and defaults for ops settings.
    Useful for displaying the ops configuration panel.
    """
    require_admin(current_user)

    try:
        # Get current values from database
        all_settings = db.get_settings_dict()

        # Build response with defaults and current values
        ops_config = {}
        for key, default_value in OPS_SETTINGS_DEFAULTS.items():
            current_value = all_settings.get(key, default_value)
            ops_config[key] = {
                "value": current_value,
                "default": default_value,
                "description": OPS_SETTINGS_DESCRIPTIONS.get(key, ""),
                "is_default": current_value == default_value
            }

        return {
            "settings": ops_config
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get ops settings: {str(e)}")


@router.put("/ops/config")
async def update_ops_settings(
    body: OpsSettingsUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Update multiple ops settings at once.

    Admin-only. Only accepts valid ops setting keys.
    Invalid keys are ignored with a warning.
    """
    require_admin(current_user)

    try:
        updated = []
        ignored = []

        for key, value in body.settings.items():
            if key in OPS_SETTINGS_DEFAULTS:
                db.set_setting(key, value)
                updated.append(key)
            else:
                ignored.append(key)

        return {
            "success": True,
            "updated": updated,
            "ignored": ignored if ignored else None
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update ops settings: {str(e)}")


@router.post("/ops/reset")
async def reset_ops_settings(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Reset all ops settings to their default values.

    Admin-only. Removes all ops settings from the database,
    causing them to fall back to defaults.
    """
    require_admin(current_user)

    try:
        deleted = []
        for key in OPS_SETTINGS_DEFAULTS.keys():
            if db.delete_setting(key):
                deleted.append(key)

        return {
            "success": True,
            "message": "Ops settings reset to defaults",
            "reset": deleted
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reset ops settings: {str(e)}")


# Note: get_ops_setting is now imported from services.settings_service
