"""Third-party credentials — Anthropic / GitHub keys and the Slack app + transport.

Carved out of the 3,529-line `routers/settings.py` (#1028). The package
`__init__` composes every sub-router onto one `/api/settings` router, so the
mounted API is byte-identical to the single-module version.
"""
import asyncio
import json
import logging
import os
import re
import httpx
from typing import List, Dict, Any
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException, Request

logger = logging.getLogger(__name__)

from models import (
    A2AOutboundEndpointUpsert,
    AgentDefaultAccessPolicyUpdate,
    AgentDefaultResourcesUpdate,
    AgentQuotaUpdate,
    ProactiveRateLimitsUpdate,
    ApiKeyTest,
    ApiKeyUpdate,
    BrainOrbSettingsUpdate,
    ElevenLabsSettingsUpdate,
    GitHubTemplatesUpdate,
    TemplateRegistryUpdate,
    MaxParallelTasksCeilingUpdate,
    McpUrlUpdate,
    OpsSettingsUpdate,
    RetentionAcknowledge,
    SkillsLibraryAutomationUpdate,
    OperatorIntakeUpdate,
    SlackConnectRequest,
    SlackSettingsUpdate,
    TelemetrySharingUpdate,
    User,
)
from database import db, SystemSetting, SystemSettingUpdate
from dependencies import get_current_user, assert_admin
from services.platform_audit_service import platform_audit_service, AuditEventType
from services import operator_intake_service, telemetry_sharing_service

# Import from settings_service (these are re-exported for backward compatibility)
from services.settings_service import (
    get_anthropic_api_key,
    get_github_pat,
    get_google_api_key,
    get_ops_setting,
    set_secret_setting,
    clear_secret_setting,
    has_secret_setting,
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
    PROACTIVE_RATE_LIMIT_DEFAULTS,
    PROACTIVE_RATE_LIMIT_DESCRIPTIONS,
    PROACTIVE_RATE_LIMIT_MAX,
    get_proactive_rate_limit,
    SKILLS_AUTO_REINJECT_ENABLED_KEY,
    SKILLS_AUTO_SYNC_ENABLED_KEY,
    SKILLS_AUTO_SYNC_INTERVAL_KEY,
    SKILLS_AUTO_SYNC_INTERVAL_DEFAULT,
    SKILLS_AUTO_SYNC_INTERVAL_MIN,
    SKILLS_AUTO_SYNC_INTERVAL_MAX,
)

# ent#236: the three keys the dedicated /skills-library route owns. Blocked on
# the generic PUT /{key} so they can only ever be written range-validated.
SKILLS_AUTOMATION_KEYS = {
    SKILLS_AUTO_SYNC_ENABLED_KEY,
    SKILLS_AUTO_SYNC_INTERVAL_KEY,
    SKILLS_AUTO_REINJECT_ENABLED_KEY,
}

# ent#346: the pre-ent#237 single-repo settings. `_adopt_legacy_clone` converts
# these into a `skill_sources` row, so writing them IS registering a skills
# source — the grant action ent#237 gates behind `reject_agent_principal` on
# every `/skills/sources` route. Blocked on the generic PUT so the gate cannot
# be walked around. `skills_library_branch` is included because a source is
# (url, ref): re-pointing the ref alone changes which commit the fleet executes.
LEGACY_SKILLS_LIBRARY_KEYS = {
    "skills_library_url",
    "skills_library_branch",
}

# ent#434 — the catch-all blocks this key in favour of the dedicated route.
from services.subscription_headroom_alerts import (
    THRESHOLD_SETTING as HEADROOM_ALERT_THRESHOLD_KEY,
    MIN_THRESHOLD_PCT as HEADROOM_THRESHOLD_MIN,
    MAX_THRESHOLD_PCT as HEADROOM_THRESHOLD_MAX,
)



router = APIRouter()


def mask_api_key(key: str) -> str:
    """Mask an API key for display, showing only last 4 characters."""
    if not key or len(key) < 8:
        return "****"
    return f"...{key[-4:]}"
@router.get("/api-keys")
async def get_api_keys_status(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get status of configured API keys.

    Admin-only. Returns masked key info for security.
    """
    assert_admin(current_user)

    try:
        # Get Anthropic key
        anthropic_key = get_anthropic_api_key()
        anthropic_configured = bool(anthropic_key)

        # Check if it's from settings or env
        key_from_settings = has_secret_setting('anthropic_api_key')

        # Get GitHub PAT
        github_pat = get_github_pat()
        github_configured = bool(github_pat)
        github_from_settings = has_secret_setting('github_pat')

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
    assert_admin(current_user)

    try:
        # Validate format
        key = body.api_key.strip()
        if not key.startswith('sk-ant-'):
            raise HTTPException(
                status_code=400,
                detail="Invalid API key format. Anthropic keys start with 'sk-ant-'"
            )

        # Store in settings — AES-256-GCM encrypted at rest (ent#435)
        set_secret_setting('anthropic_api_key', key)

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
    assert_admin(current_user)

    try:
        deleted = clear_secret_setting('anthropic_api_key')

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
    assert_admin(current_user)

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
    assert_admin(current_user)

    try:
        # Validate format
        key = body.api_key.strip()
        if not (key.startswith('ghp_') or key.startswith('github_pat_')):
            raise HTTPException(
                status_code=400,
                detail="Invalid token format. GitHub PATs start with 'ghp_' or 'github_pat_'"
            )

        # Store in settings
        set_secret_setting('github_pat', key)

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
    assert_admin(current_user)

    try:
        deleted = clear_secret_setting('github_pat')

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
    assert_admin(current_user)

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
@router.get("/slack")
async def get_slack_settings_status(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get status of Slack integration settings.

    Admin-only. Returns masked key info for security.
    """
    assert_admin(current_user)

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
        client_secret_from_settings = has_secret_setting('slack_client_secret')
        signing_secret_from_settings = has_secret_setting('slack_signing_secret')

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
    assert_admin(current_user)

    try:
        updated = []

        if body.client_id is not None:
            db.set_setting('slack_client_id', body.client_id.strip())
            updated.append('client_id')

        if body.client_secret is not None:
            set_secret_setting('slack_client_secret', body.client_secret)
            updated.append('client_secret')

        if body.signing_secret is not None:
            set_secret_setting('slack_signing_secret', body.signing_secret)
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
    assert_admin(current_user)

    try:
        deleted = []
        # client_id is a public identifier (plain row); the two secrets are
        # encrypted, so clearing them must remove BOTH forms (ent#435).
        if db.delete_setting('slack_client_id'):
            deleted.append('slack_client_id')
        for key in ['slack_client_secret', 'slack_signing_secret']:
            if clear_secret_setting(key):
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
    assert_admin(current_user)

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
    assert_admin(current_user)

    # Save settings to DB
    if body.app_token is not None:
        set_secret_setting("slack_app_token", body.app_token)
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
    assert_admin(current_user)

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
    assert_admin(current_user)

    transport = getattr(request.app.state, 'slack_transport', None)
    if not transport:
        return {"disconnected": True, "was_connected": False}

    try:
        await transport.stop()
    except Exception as e:
        logger.warning(f"Error stopping Slack transport: {e}")

    request.app.state.slack_transport = None

    return {"disconnected": True, "was_connected": True}
