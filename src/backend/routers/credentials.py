"""
Credential management routes for the Trinity backend.
"""
import os
from datetime import datetime
from pathlib import Path
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request
import httpx
import yaml

from models import (
    User,
    BulkCredentialImport,
    BulkCredentialResult,
    HotReloadCredentialsRequest,
    CredentialAssignRequest,
    CredentialBulkAssignRequest,
)
from config import OAUTH_CONFIGS, BACKEND_URL
from dependencies import get_current_user
from services.docker_service import get_agent_container, get_agent_status_from_container
from services.template_service import (
    get_github_template,
    extract_agent_credentials,
    generate_credential_files,
    find_template_file,
)
from utils.helpers import parse_env_content, infer_service_from_key, infer_type_from_key
from credentials import (
    CredentialManager,
    CredentialCreate,
    CredentialUpdate,
    Credential,
    OAuthConfig
)

router = APIRouter(prefix="/api", tags=["credentials"])

# Initialize credential manager
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
credential_manager = CredentialManager(REDIS_URL)


@router.post("/credentials", response_model=Credential)
async def create_credential(
    cred_data: CredentialCreate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Create a new credential."""
    try:
        credential = credential_manager.create_credential(current_user.username, cred_data)

        return credential
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create credential: {str(e)}")


@router.get("/credentials", response_model=List[Credential])
async def list_credentials(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """List all credentials for the current user."""
    try:
        credentials = credential_manager.list_credentials(current_user.username)

        return credentials
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list credentials: {str(e)}")


@router.get("/credentials/{cred_id}", response_model=Credential)
async def get_credential(
    cred_id: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get a specific credential."""
    credential = credential_manager.get_credential(cred_id, current_user.username)

    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")

    return credential


@router.put("/credentials/{cred_id}", response_model=Credential)
async def update_credential(
    cred_id: str,
    update_data: CredentialUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Update a credential."""
    credential = credential_manager.update_credential(cred_id, current_user.username, update_data)

    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")

    return credential


@router.delete("/credentials/{cred_id}")
async def delete_credential(
    cred_id: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Delete a credential."""
    success = credential_manager.delete_credential(cred_id, current_user.username)

    if not success:
        raise HTTPException(status_code=404, detail="Credential not found")

    return {"message": f"Credential {cred_id} deleted"}


@router.post("/credentials/bulk", response_model=BulkCredentialResult)
async def bulk_import_credentials(
    import_data: BulkCredentialImport,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Bulk import credentials from .env-style text content."""
    parsed = parse_env_content(import_data.content)

    if not parsed:
        raise HTTPException(
            status_code=400,
            detail="No valid KEY=VALUE pairs found in content"
        )

    created_count = 0
    skipped_count = 0
    errors = []
    created_credentials = []

    for key, value in parsed:
        try:
            service = infer_service_from_key(key)
            cred_type = infer_type_from_key(key)

            if cred_type == 'token':
                credentials_data = {'token': value}
            else:
                credentials_data = {'api_key': value}

            credentials_data[key] = value

            cred_data = CredentialCreate(
                name=key,
                service=service,
                type=cred_type,
                credentials=credentials_data,
                description=f"Bulk imported credential"
            )

            credential = credential_manager.create_credential(current_user.username, cred_data)
            created_count += 1
            created_credentials.append({
                "id": credential.id,
                "name": credential.name,
                "service": credential.service,
                "type": credential.type
            })

        except Exception as e:
            errors.append(f"{key}: {str(e)}")
            skipped_count += 1

    return BulkCredentialResult(
        created=created_count,
        skipped=skipped_count,
        errors=errors,
        credentials=created_credentials
    )


# OAuth Routes
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

    redirect_uri = f"{BACKEND_URL}/api/oauth/{provider}/callback"
    state = credential_manager.create_oauth_state(current_user.username, provider, redirect_uri)

    auth_url = credential_manager.build_oauth_url(
        provider,
        config["client_id"],
        redirect_uri,
        state
    )

    return {"auth_url": auth_url, "state": state}


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    code: str,
    state: str,
    request: Request
):
    """OAuth callback handler."""
    state_data = credential_manager.verify_oauth_state(state)

    if not state_data:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    if state_data["provider"] != provider:
        raise HTTPException(status_code=400, detail="Provider mismatch")

    config = OAUTH_CONFIGS[provider]

    tokens = await credential_manager.exchange_oauth_code(
        provider,
        code,
        config["client_id"],
        config["client_secret"],
        state_data["redirect_uri"]
    )

    if not tokens:
        raise HTTPException(status_code=500, detail="Failed to exchange OAuth code")

    # Normalize credential names for MCP compatibility
    normalized_creds = {
        # Original OAuth response fields
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "token_type": tokens.get("token_type"),
        "expires_in": tokens.get("expires_in"),
        "scope": tokens.get("scope"),
        "client_id": tokens.get("client_id"),
        "client_secret": tokens.get("client_secret"),
    }

    # Add MCP-compatible naming for each provider
    if provider == "google":
        normalized_creds.update({
            "GOOGLE_ACCESS_TOKEN": tokens.get("access_token"),
            "GOOGLE_REFRESH_TOKEN": tokens.get("refresh_token"),
            "GOOGLE_CLIENT_ID": tokens.get("client_id"),
            "GOOGLE_CLIENT_SECRET": tokens.get("client_secret"),
        })
    elif provider == "slack":
        normalized_creds.update({
            "SLACK_ACCESS_TOKEN": tokens.get("access_token"),
            "SLACK_BOT_TOKEN": tokens.get("bot_token"),
            "SLACK_CLIENT_ID": tokens.get("client_id"),
            "SLACK_CLIENT_SECRET": tokens.get("client_secret"),
        })
    elif provider == "github":
        normalized_creds.update({
            "GITHUB_ACCESS_TOKEN": tokens.get("access_token"),
            "GITHUB_TOKEN": tokens.get("access_token"),
            "GITHUB_CLIENT_ID": tokens.get("client_id"),
            "GITHUB_CLIENT_SECRET": tokens.get("client_secret"),
        })
    elif provider == "notion":
        normalized_creds.update({
            "NOTION_ACCESS_TOKEN": tokens.get("access_token"),
            "NOTION_TOKEN": tokens.get("access_token"),
            "NOTION_CLIENT_ID": tokens.get("client_id"),
            "NOTION_CLIENT_SECRET": tokens.get("client_secret"),
        })

    cred_data = CredentialCreate(
        name=f"{provider.title()} OAuth - {datetime.now().strftime('%Y-%m-%d')}",
        service=provider,
        type="oauth2",
        credentials=normalized_creds,
        description=f"OAuth connection created via authorization flow"
    )

    credential = credential_manager.create_credential(state_data["user_id"], cred_data)

    return {
        "message": "OAuth authentication successful",
        "credential_id": credential.id,
        "redirect": "http://localhost:3000/credentials"
    }


@router.get("/oauth/providers")
async def list_oauth_providers():
    """List available OAuth providers."""
    providers = []

    for provider, config in OAUTH_CONFIGS.items():
        providers.append({
            "name": provider,
            "display_name": provider.title(),
            "configured": bool(config["client_id"]),
            "scopes": OAuthConfig.PROVIDERS[provider]["scopes"]
        })

    return {"providers": providers}


# Agent Credential Routes
@router.get("/agents/{agent_name}/credentials")
async def get_agent_credentials_requirements(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get credential requirements for an agent."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    labels = container.labels
    template_id = labels.get("trinity.template", "")

    if not template_id:
        return {
            "agent_name": agent_name,
            "template": None,
            "required_credentials": [],
            "message": "No template associated with this agent"
        }

    required_credentials = []

    if template_id.startswith("github:"):
        gh_template = get_github_template(template_id)
        if gh_template:
            required_credentials = gh_template.get("required_credentials", [])
    else:
        templates_dir = Path("/agent-configs/templates")
        if not templates_dir.exists():
            templates_dir = Path("./config/agent-templates")

        template_path = templates_dir / template_id
        if template_path.exists():
            creds_info = extract_agent_credentials(template_path)
            required_credentials = creds_info.get("required_credentials", [])

    all_credentials = credential_manager.list_credentials(current_user.username)
    configured_cred_names = set()

    for cred in all_credentials:
        configured_cred_names.add(cred.name.upper())
        cred_data = credential_manager.get_credential_secret(cred.id, current_user.username)
        if cred_data:
            for key in cred_data.keys():
                configured_cred_names.add(key.upper())
                configured_cred_names.add(f"{cred.service.upper()}_{key.upper()}")

    credentials_with_status = []
    for cred in required_credentials:
        cred_name = cred["name"]
        is_configured = cred_name in configured_cred_names

        credentials_with_status.append({
            "name": cred_name,
            "source": cred.get("source", "unknown"),
            "configured": is_configured
        })

    return {
        "agent_name": agent_name,
        "template": template_id,
        "required_credentials": credentials_with_status,
        "total": len(credentials_with_status),
        "configured_count": sum(1 for c in credentials_with_status if c["configured"]),
        "missing_count": sum(1 for c in credentials_with_status if not c["configured"])
    }


@router.post("/agents/{agent_name}/credentials/reload")
async def reload_agent_credentials(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Reload credentials on a running agent from Redis store."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent_status = get_agent_status_from_container(container)
    if agent_status.status != "running":
        raise HTTPException(
            status_code=400,
            detail=f"Agent is not running (status: {agent_status.status}). Start the agent first."
        )

    template_id = agent_status.template or ""
    mcp_servers = []

    template_data = {}
    mcp_template_content = None

    if template_id:
        if template_id.startswith("github:"):
            pass
        else:
            templates_dir = Path("/agent-configs/templates")
            if not templates_dir.exists():
                templates_dir = Path("./config/agent-templates")

            template_path = find_template_file(templates_dir / template_id)
            if template_path:
                with open(template_path) as f:
                    template_data = yaml.safe_load(f)

            mcp_template_path = templates_dir / template_id / ".mcp.json"
            if mcp_template_path.exists():
                with open(mcp_template_path) as f:
                    mcp_template_content = f.read()

    # Get only explicitly assigned credentials
    agent_credentials = credential_manager.get_assigned_credential_values(agent_name, current_user.username)

    mcp_config = None
    if mcp_template_content and template_data:
        generated_files = generate_credential_files(template_data, agent_credentials, agent_name)
        mcp_config = generated_files.get(".mcp.json")

    # Get only explicitly assigned file-type credentials
    file_credentials = credential_manager.get_assigned_file_credentials(agent_name, current_user.username)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://agent-{agent_name}:8000/api/credentials/update",
                json={
                    "credentials": agent_credentials,
                    "mcp_config": mcp_config,
                    "files": file_credentials if file_credentials else None
                },
                timeout=30.0
            )

            if response.status_code != 200:
                error_detail = response.json().get("detail", "Unknown error")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Agent rejected credential update: {error_detail}"
                )

            agent_response = response.json()

    except httpx.RequestError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to agent: {str(e)}"
        )

    return {
        "message": f"Credentials reloaded on agent {agent_name}",
        "credential_count": len(agent_credentials),
        "updated_files": agent_response.get("updated_files", []),
        "note": agent_response.get("note", "")
    }


@router.get("/agents/{agent_name}/credentials/status")
async def get_agent_credentials_status(
    agent_name: str,
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
        async with httpx.AsyncClient() as client:
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


@router.post("/agents/{agent_name}/credentials/hot-reload")
async def hot_reload_credentials(
    agent_name: str,
    request_body: HotReloadCredentialsRequest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Hot-reload credentials on a running agent by parsing .env-style text.

    This endpoint:
    1. Parses .env-style KEY=VALUE text
    2. Saves each credential to Redis (with conflict resolution)
    3. Pushes credentials to the running agent's .env file

    Credentials are persisted in Redis so they survive agent restarts.
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

    # Parse credentials from text
    credentials = {}
    for line in request_body.credentials_text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or \
               (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            if key:
                credentials[key] = value

    if not credentials:
        raise HTTPException(
            status_code=400,
            detail="No valid credentials found in the provided text"
        )

    # Save credentials to Redis with conflict resolution AND assign to this agent
    # This ensures hot-reloaded credentials persist across agent restarts
    saved_credentials = []
    for key, value in credentials.items():
        result = credential_manager.import_credential_with_conflict_resolution(
            key, value, current_user.username
        )
        saved_credentials.append({
            "name": result["name"],
            "status": result["status"],
            "original": result.get("original")
        })

        # Also assign the credential to this agent
        # Find the credential by name to get its ID
        cred = credential_manager.get_credential_by_name(result["name"], current_user.username)
        if cred:
            credential_manager.assign_credential(agent_name, cred.id, current_user.username)

    # Get only assigned file-type credentials (not all user credentials)
    file_credentials = credential_manager.get_assigned_file_credentials(agent_name, current_user.username)

    # Push credentials to the running agent
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://agent-{agent_name}:8000/api/credentials/update",
                json={
                    "credentials": credentials,
                    "mcp_config": None,
                    "files": file_credentials if file_credentials else None
                },
                timeout=30.0
            )

            if response.status_code != 200:
                error_detail = response.json().get("detail", "Unknown error")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Agent rejected credential update: {error_detail}"
                )

            agent_response = response.json()

    except httpx.RequestError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to agent: {str(e)}"
        )

    return {
        "message": f"Hot-reloaded {len(credentials)} credentials on agent {agent_name}",
        "credential_count": len(credentials),
        "credential_names": list(credentials.keys()),
        "updated_files": agent_response.get("updated_files", []),
        "saved_to_redis": saved_credentials,
        "note": agent_response.get("note", "")
    }


# ============================================================================
# Agent Credential Assignment Endpoints
# ============================================================================

@router.get("/agents/{agent_name}/credentials/assignments")
async def get_credential_assignments(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get all credentials with assignment status for an agent.

    Returns two lists:
    - assigned: credentials currently assigned to this agent
    - available: credentials owned by user but not assigned to this agent
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get assigned credential IDs
    assigned_ids = credential_manager.get_assigned_credential_ids(agent_name)

    # Get all user credentials
    all_credentials = credential_manager.list_credentials(current_user.username)

    assigned = []
    available = []

    for cred in all_credentials:
        cred_info = {
            "id": cred.id,
            "name": cred.name,
            "service": cred.service,
            "type": cred.type,
            "file_path": cred.file_path
        }

        if cred.id in assigned_ids:
            assigned.append(cred_info)
        else:
            available.append(cred_info)

    return {
        "agent_name": agent_name,
        "assigned": assigned,
        "available": available
    }


@router.post("/agents/{agent_name}/credentials/assign")
async def assign_credential(
    agent_name: str,
    request_body: CredentialAssignRequest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Assign a credential to an agent."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    success = credential_manager.assign_credential(
        agent_name,
        request_body.credential_id,
        current_user.username
    )

    if not success:
        raise HTTPException(status_code=404, detail="Credential not found")

    return {
        "message": "Credential assigned to agent",
        "credential_id": request_body.credential_id,
        "agent_name": agent_name
    }


@router.delete("/agents/{agent_name}/credentials/assign/{cred_id}")
async def unassign_credential(
    agent_name: str,
    cred_id: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Unassign a credential from an agent."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    success = credential_manager.unassign_credential(agent_name, cred_id)

    if not success:
        raise HTTPException(status_code=404, detail="Credential assignment not found")

    return {
        "message": "Credential unassigned from agent",
        "credential_id": cred_id,
        "agent_name": agent_name
    }


@router.post("/agents/{agent_name}/credentials/assign/bulk")
async def bulk_assign_credentials(
    agent_name: str,
    request_body: CredentialBulkAssignRequest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Assign multiple credentials to an agent at once."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    assigned = []
    failed = []

    for cred_id in request_body.credential_ids:
        success = credential_manager.assign_credential(
            agent_name,
            cred_id,
            current_user.username
        )
        if success:
            assigned.append(cred_id)
        else:
            failed.append(cred_id)

    return {
        "message": f"Assigned {len(assigned)} credentials to agent",
        "assigned_count": len(assigned),
        "assigned_ids": assigned,
        "failed_ids": failed
    }


@router.post("/agents/{agent_name}/credentials/apply")
async def apply_credentials(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Apply assigned credentials to a running agent.

    Pushes all assigned credentials (env vars and files) to the agent.
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

    # Get assigned credentials (env vars)
    credentials = credential_manager.get_assigned_credential_values(agent_name, current_user.username)

    # Get assigned file credentials
    file_credentials = credential_manager.get_assigned_file_credentials(agent_name, current_user.username)

    # Get template data for .mcp.json generation
    template_id = agent_status.template or ""
    mcp_config = None

    if template_id and not template_id.startswith("github:"):
        templates_dir = Path("/agent-configs/templates")
        if not templates_dir.exists():
            templates_dir = Path("./config/agent-templates")

        template_path = find_template_file(templates_dir / template_id)
        if template_path:
            with open(template_path) as f:
                template_data = yaml.safe_load(f)

            if template_data:
                generated_files = generate_credential_files(template_data, credentials, agent_name)
                mcp_config = generated_files.get(".mcp.json")

    # Push to agent
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://agent-{agent_name}:8000/api/credentials/update",
                json={
                    "credentials": credentials,
                    "mcp_config": mcp_config,
                    "files": file_credentials if file_credentials else None
                },
                timeout=30.0
            )

            if response.status_code != 200:
                error_detail = response.json().get("detail", "Unknown error")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Agent rejected credential update: {error_detail}"
                )

            agent_response = response.json()

    except httpx.RequestError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to agent: {str(e)}"
        )

    return {
        "message": f"Applied {len(credentials)} credentials to agent {agent_name}",
        "credential_count": len(credentials),
        "file_count": len(file_credentials),
        "file_paths": list(file_credentials.keys()) if file_credentials else [],
        "updated_files": agent_response.get("updated_files", []),
        "files_written": agent_response.get("files_written", []),
        "note": agent_response.get("note", "")
    }


# ============================================================================
# Legacy MCP Server Credential Assignment (must be AFTER /assign routes)
# ============================================================================

@router.post("/agents/{agent_name}/credentials/{mcp_server}")
async def assign_credential_to_mcp_server(
    agent_name: str,
    mcp_server: str,
    cred_id: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Assign a credential to an agent for a specific MCP server (legacy)."""
    from services.docker_service import get_agent_by_name

    if not get_agent_by_name(agent_name):
        raise HTTPException(status_code=404, detail="Agent not found")

    success = credential_manager.assign_credential_to_agent(
        agent_name,
        mcp_server,
        cred_id,
        current_user.username
    )

    if not success:
        raise HTTPException(status_code=404, detail="Credential not found")

    return {"message": f"Credential assigned to {agent_name} for {mcp_server}"}
