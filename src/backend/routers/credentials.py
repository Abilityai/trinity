"""
Credential management routes for the Trinity backend.

CRED-002: Simplified credential system using encrypted files in git.
The old Redis-based credential system has been removed. Credentials are now:
1. Injected directly into agents via inject_credentials
2. Exported to encrypted .credentials.enc files (can be committed to git)
3. Imported from .credentials.enc files on agent startup
"""
import logging
import os
from fastapi import APIRouter, Depends, HTTPException, Request
import httpx

from models import (
    User,
    CredentialInjectRequest,
    CredentialInjectResponse,
    CredentialExportResponse,
    CredentialImportResponse,
)
from config import OAUTH_CONFIGS, BACKEND_URL
from dependencies import get_current_user, require_admin, get_authorized_agent_by_name, get_owned_agent_by_name
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container, get_agent_status_from_container
from services.mcp_validator import validate_mcp_config, McpValidationError
from services.platform_audit_service import platform_audit_service, AuditEventType
# Curated credential-path policy (#11) — widens the original exact allowlist
# (#183/#590/#598) to a vetted set of credential file *types* while still
# blocking anything executed/sourced at startup. Single source of truth, also
# vendored into the agent image (Invariant #5, byte-identical parity test).
from services.credential_paths import disallowed_paths

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["credentials"])

# `.mcp.json` passes the PATH layer but its CONTENT is still structure-validated
# by `services.mcp_validator.validate_mcp_config` before the inject is accepted
# (#598, Layer 2 of AISEC-C2 closure). `.mcp.json.template` stays BLOCKED by the
# policy because the envsubst flow it feeds doesn't sanitize attacker JSON.


# ============================================================================
# Agent Credential Status (Simplified)
# ============================================================================

@router.get("/agents/{agent_name}/credentials/status")
async def get_agent_credentials_status(
    agent_name: str = Depends(get_authorized_agent_by_name),
    current_user: User = Depends(get_current_user)
):
    """Get credential status from a running agent."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent_status = get_agent_status_from_container(container)
    if agent_status.status != "running":
        return {
            "agent_name": agent_name,
            "status": "agent_not_running",
            "message": "Agent must be running to check credential status"
        }

    try:
        async with agent_httpx_client(agent_name) as client:
            response = await client.get(
                f"http://agent-{agent_name}:8000/api/credentials/status",
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()

    except httpx.RequestError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to agent: {str(e)}"
        )


# ============================================================================
# OAuth Routes (kept for OAuth flow support)
# ============================================================================

@router.post("/oauth/{provider}/init")
async def init_oauth(
    provider: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Initialize OAuth flow for a provider."""
    if provider not in OAUTH_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Unsupported OAuth provider: {provider}")

    config = OAUTH_CONFIGS[provider]
    if not config["client_id"]:
        raise HTTPException(
            status_code=500,
            detail=f"OAuth not configured for {provider}. Set {provider.upper()}_CLIENT_ID environment variable."
        )

    # OAuth state is now stored in a simple in-memory dict (ephemeral)
    # For production, consider using Redis or database
    import secrets
    import json
    state = secrets.token_urlsafe(32)

    # Store state in environment-based cache (simplified)
    # In production, use Redis or database
    redirect_uri = f"{BACKEND_URL}/api/oauth/{provider}/callback"

    # Build OAuth URL based on provider
    oauth_urls = {
        "google": "https://accounts.google.com/o/oauth2/v2/auth",
        "github": "https://github.com/login/oauth/authorize",
        "slack": "https://slack.com/oauth/v2/authorize",
        "notion": "https://api.notion.com/v1/oauth/authorize",
    }

    scopes = {
        "google": "openid email profile https://www.googleapis.com/auth/drive",
        "github": "repo user",
        "slack": "chat:write channels:read",
        "notion": "",
    }

    auth_url = (
        f"{oauth_urls.get(provider)}?"
        f"client_id={config['client_id']}&"
        f"redirect_uri={redirect_uri}&"
        f"response_type=code&"
        f"state={state}&"
        f"scope={scopes.get(provider, '')}"
    )

    return {"auth_url": auth_url, "state": state}


@router.get("/oauth/providers")
async def list_oauth_providers(current_user: User = Depends(get_current_user)):
    """List available OAuth providers. Requires authentication (SEC-180)."""
    oauth_scopes = {
        "google": ["openid", "email", "profile", "drive"],
        "github": ["repo", "user"],
        "slack": ["chat:write", "channels:read"],
        "notion": [],
    }

    providers = []
    for provider, config in OAUTH_CONFIGS.items():
        providers.append({
            "name": provider,
            "display_name": provider.title(),
            "configured": bool(config["client_id"]),
            "scopes": oauth_scopes.get(provider, [])
        })

    return {"providers": providers}


# ============================================================================
# New Credential System (CRED-002: Simplified Encrypted File Storage)
# ============================================================================

@router.post("/agents/{agent_name}/credentials/inject")
async def inject_credentials(
    request_body: CredentialInjectRequest,
    request: Request,
    agent_name: str = Depends(get_owned_agent_by_name),
    current_user: User = Depends(get_current_user)
):
    """
    Inject credential files directly into a running agent.

    This is the new simplified credential injection that writes files
    directly to the agent's workspace without Redis or template processing.

    Args:
        agent_name: Name of the agent
        request_body: Contains files dict mapping paths to contents
                     e.g., {".env": "KEY=value", ".mcp.json": "{}"}
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent_status = get_agent_status_from_container(container)
    if agent_status.status != "running":
        raise HTTPException(
            status_code=400,
            detail=f"Agent is not running (status: {agent_status.status}). Start the agent first."
        )

    if not request_body.files and not request_body.files_b64:
        raise HTTPException(status_code=400, detail="No files provided for injection")

    # Validate all file paths against the curated policy (#183/#590/#598/#11).
    # Both the text (`files`) and binary (`files_b64`) maps are gated.
    overlap = set(request_body.files) & set(request_body.files_b64)
    if overlap:
        raise HTTPException(
            status_code=400,
            detail=f"Path(s) given as both text and binary: {sorted(overlap)}",
        )
    disallowed = disallowed_paths(list(request_body.files) + list(request_body.files_b64))
    if disallowed:
        logger.warning(
            f"Credential injection blocked: disallowed paths {disallowed} "
            f"for agent {agent_name} by user {current_user.email}"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Disallowed credential file path(s): {disallowed}. "
                   f"Only curated credential file types are permitted (no shell/agent "
                   f"startup files, no path traversal).",
        )

    # #11 review: `.mcp.json` is a structure-validated TEXT config. It must NOT
    # arrive via the binary (`files_b64`) channel, which would skip the
    # validate_mcp_config guard below and let an attacker reintroduce the #590
    # RCE-by-config (a stdio `command` MCP server) through base64.
    if ".mcp.json" in request_body.files_b64:
        raise HTTPException(
            status_code=400,
            detail=".mcp.json must be injected as text (files), not binary (files_b64), "
                   "so its content can be validated.",
        )

    # AISEC-C2 / #590 Layer 2 (#598): structure-validate .mcp.json content
    # before forwarding to the agent. Closes the RCE-by-config bypass while
    # restoring the legitimate post-deploy MCP server editing flow.
    #
    # The validator special-cases the auto-injected `trinity` entry: it
    # accepts the canonical Trinity-MCP shape (so owners can rotate their
    # bearer token) but rejects any other shape under that name (so
    # attackers can't redefine trinity as a stdio shell-injection vector).
    if ".mcp.json" in request_body.files:
        try:
            validate_mcp_config(request_body.files[".mcp.json"])
        except McpValidationError as e:
            logger.warning(
                f".mcp.json injection blocked by validator for agent {agent_name} "
                f"by user {current_user.email}: {e}"
            )
            raise HTTPException(
                status_code=400,
                detail=f"Invalid .mcp.json: {e}",
            )

    try:
        async with agent_httpx_client(agent_name) as client:
            response = await client.post(
                f"http://agent-{agent_name}:8000/api/credentials/inject",
                json={"files": request_body.files, "files_b64": request_body.files_b64},
                timeout=30.0
            )

            if response.status_code != 200:
                error_detail = response.json().get("detail", "Unknown error")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Agent rejected credential injection: {error_detail}"
                )

            agent_response = response.json()

    except httpx.RequestError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to agent: {str(e)}"
        )

    # SEC-001: audit credential injection
    await platform_audit_service.log(
        event_type=AuditEventType.CREDENTIALS,
        event_action="inject",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"files": list(request_body.files.keys()) + list(request_body.files_b64.keys())},
    )

    all_paths = list(request_body.files.keys()) + list(request_body.files_b64.keys())
    return CredentialInjectResponse(
        status="success",
        files_written=agent_response.get("files_written", all_paths),
        message=f"Injected {len(all_paths)} credential file(s) to agent {agent_name}"
    )


@router.post("/agents/{agent_name}/credentials/export")
async def export_credentials(
    request: Request,
    agent_name: str = Depends(get_owned_agent_by_name),
    current_user: User = Depends(get_current_user)
):
    """
    Export credentials from agent to encrypted .credentials.enc file.

    Reads credential files (.env, .mcp.json, etc.) from the agent,
    encrypts them, and writes .credentials.enc to the agent's workspace.
    This file can be committed to git for portable credential storage.
    """
    from services.credential_encryption import get_credential_encryption_service

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent_status = get_agent_status_from_container(container)
    if agent_status.status != "running":
        raise HTTPException(
            status_code=400,
            detail=f"Agent is not running (status: {agent_status.status}). Start the agent first."
        )

    try:
        encryption_service = get_credential_encryption_service()
        # export_to_agent returns (path, count) — the count is the FULL captured
        # set (#11), not a stale re-read of just the 2 defaults.
        encrypted_file, files_exported = await encryption_service.export_to_agent(agent_name)

        # SEC-001: audit credential export
        await platform_audit_service.log(
            event_type=AuditEventType.CREDENTIALS,
            event_action="export",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            target_type="agent",
            target_id=agent_name,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"files_exported": files_exported},
        )

        return CredentialExportResponse(
            status="success",
            encrypted_file=encrypted_file,
            files_exported=files_exported
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except httpx.RequestError as e:
        # Same transient-connectivity mapping as `inject_credentials` —
        # the agent container is up but its FastAPI server is not yet
        # reachable. Surfaces 503 instead of 500.
        logger.warning(
            f"Credential export for agent {agent_name} could not reach "
            f"agent server: {e}"
        )
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to agent: {str(e)}"
        )
    except Exception as e:
        logger.exception(
            f"Credential export for agent {agent_name} failed unexpectedly"
        )
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@router.post("/agents/{agent_name}/credentials/import")
async def import_credentials(
    request: Request,
    agent_name: str = Depends(get_owned_agent_by_name),
    current_user: User = Depends(get_current_user)
):
    """
    Import credentials from encrypted .credentials.enc file to agent.

    Reads .credentials.enc from the agent's workspace, decrypts it,
    and writes the credential files (.env, .mcp.json, etc.) to the workspace.
    """
    from services.credential_encryption import get_credential_encryption_service

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent_status = get_agent_status_from_container(container)
    if agent_status.status != "running":
        raise HTTPException(
            status_code=400,
            detail=f"Agent is not running (status: {agent_status.status}). Start the agent first."
        )

    try:
        encryption_service = get_credential_encryption_service()
        files = await encryption_service.import_to_agent(agent_name)

        # SEC-001: audit credential import
        await platform_audit_service.log(
            event_type=AuditEventType.CREDENTIALS,
            event_action="import",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            target_type="agent",
            target_id=agent_name,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"files_imported": list(files.keys())},
        )

        return CredentialImportResponse(
            status="success",
            files_imported=list(files.keys()),
            message=f"Imported {len(files)} credential file(s) from .credentials.enc"
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except httpx.RequestError as e:
        # Agent container is up but its internal FastAPI server is not yet
        # reachable (startup race, connect refused, read timeout, etc.).
        # Mirrors the 503 mapping already used by `inject_credentials` and
        # `routers/agent_files.py` so the credentials surface stays
        # consistent — these are transient infrastructure failures, not
        # programmer errors that warrant a 500.
        logger.warning(
            f"Credential import for agent {agent_name} could not reach "
            f"agent server: {e}"
        )
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to agent: {str(e)}"
        )
    except Exception as e:
        logger.exception(
            f"Credential import for agent {agent_name} failed unexpectedly"
        )
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")


@router.get("/credentials/encryption-key")
async def get_encryption_key(
    request: Request,
    current_user: User = Depends(require_admin)
):
    """
    Get the platform's credential encryption key.
    Enables local agents to encrypt/decrypt .credentials.enc files.

    Security: Admin-only access (C-001).
    """
    key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if not key:
        raise HTTPException(
            status_code=503,
            detail="Credential encryption key not configured"
        )

    return {
        "key": key,
        "algorithm": "AES-256-GCM",
        "key_format": "hex (64 characters)",
        "note": "Store securely. Never commit to git."
    }
