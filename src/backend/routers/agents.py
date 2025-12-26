"""
Agent management routes for the Trinity backend.
"""
import os
import json
import docker
import select
import asyncio
import threading
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Query, WebSocket, WebSocketDisconnect
import yaml

from models import AgentConfig, AgentStatus, User
from database import db
from dependencies import get_current_user, decode_token
from services.audit_service import log_audit_event
from services.docker_service import (
    docker_client,
    get_agent_container,
    get_agent_status_from_container,
    list_all_agents,
    get_agent_by_name,
    get_next_available_port,
)
from services.template_service import (
    get_github_template,
    generate_credential_files,
)
from services.scheduler_service import scheduler_service
from services.execution_queue import get_execution_queue
from services import git_service
from utils.helpers import sanitize_agent_name
from credentials import CredentialManager
from routers.settings import get_anthropic_api_key

router = APIRouter(prefix="/api/agents", tags=["agents"])

# Initialize credential manager
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
credential_manager = CredentialManager(REDIS_URL)

# WebSocket manager will be injected from main.py
manager = None

# Logger for terminal sessions
logger = logging.getLogger(__name__)

# Track active terminal sessions per agent (limit 1 per user per agent)
_active_terminal_sessions: dict = {}  # (user_id, agent_name) -> session_info
_terminal_lock = threading.Lock()

def set_websocket_manager(ws_manager):
    """Set the WebSocket manager for broadcasting events."""
    global manager
    manager = ws_manager


async def inject_trinity_meta_prompt(agent_name: str, max_retries: int = 5, retry_delay: float = 2.0) -> dict:
    """
    Inject Trinity meta-prompt into an agent via its internal API.

    This is called after agent startup to inject planning commands
    and create the necessary directory structure.

    Args:
        agent_name: Name of the agent
        max_retries: Number of retries for connection
        retry_delay: Seconds between retries

    Returns:
        dict with injection status or error info
    """
    import httpx
    import asyncio
    import logging

    logger = logging.getLogger(__name__)
    agent_url = f"http://agent-{agent_name}:8000"

    # Fetch system-wide custom prompt setting
    custom_prompt = db.get_setting_value("trinity_prompt", default=None)
    if custom_prompt:
        logger.info(f"Found trinity_prompt setting ({len(custom_prompt)} chars), will inject into {agent_name}")

    # Build request payload
    payload = {"force": False}
    if custom_prompt:
        payload["custom_prompt"] = custom_prompt

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{agent_url}/api/trinity/inject",
                    json=payload
                )

                if response.status_code == 200:
                    result = response.json()
                    logger.info(f"Trinity injection successful for {agent_name}: {result.get('status')}")
                    return result
                else:
                    logger.warning(f"Trinity injection returned {response.status_code}: {response.text}")
                    return {"status": "error", "error": f"HTTP {response.status_code}: {response.text}"}

        except httpx.ConnectError:
            # Agent server not ready yet - retry
            if attempt < max_retries - 1:
                logger.info(f"Agent {agent_name} not ready yet (attempt {attempt + 1}/{max_retries}), retrying...")
                await asyncio.sleep(retry_delay)
            else:
                logger.warning(f"Could not connect to agent {agent_name} after {max_retries} attempts")
                return {"status": "error", "error": "Agent server not reachable after retries"}

        except Exception as e:
            logger.error(f"Trinity injection error for {agent_name}: {e}")
            return {"status": "error", "error": str(e)}

    return {"status": "error", "error": "Max retries exceeded"}


async def start_agent_internal(agent_name: str) -> dict:
    """
    Internal function to start an agent.

    Used by both the API endpoint and system deployment.
    Triggers Trinity meta-prompt injection.

    Args:
        agent_name: Name of the agent to start

    Returns:
        dict with start status and trinity_injection result

    Raises:
        HTTPException: If agent not found or start fails
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Check if container needs recreation for shared folders or API key settings
    container.reload()
    needs_recreation = (
        not _check_shared_folder_mounts_match(container, agent_name) or
        not _check_api_key_env_matches(container, agent_name)
    )

    if needs_recreation:
        # Recreate container with updated config
        # Use system user for internal operations
        await _recreate_container_with_updated_config(agent_name, container, "system")
        container = get_agent_container(agent_name)

    container.start()

    # Inject Trinity meta-prompt
    trinity_result = await inject_trinity_meta_prompt(agent_name)
    trinity_status = trinity_result.get("status", "unknown")

    return {
        "message": f"Agent {agent_name} started",
        "trinity_injection": trinity_status,
        "trinity_result": trinity_result
    }


def get_accessible_agents(current_user: User):
    """
    Get list of all agents accessible to the current user.

    Helper function for use by other routers that need agent access control.
    Returns list of agent dictionaries with ownership metadata.
    """
    all_agents = list_all_agents()
    user_data = db.get_user_by_username(current_user.username)
    is_admin = user_data and user_data["role"] == "admin"

    accessible_agents = []
    for agent in all_agents:
        agent_dict = agent.dict() if hasattr(agent, 'dict') else dict(agent)
        agent_name = agent_dict.get("name")

        if not db.can_user_access_agent(current_user.username, agent_name):
            continue

        owner = db.get_agent_owner(agent_name)
        agent_dict["owner"] = owner["owner_username"] if owner else None
        agent_dict["is_owner"] = owner and owner["owner_username"] == current_user.username
        agent_dict["is_shared"] = not agent_dict["is_owner"] and not is_admin and \
                                   db.is_agent_shared_with_user(agent_name, current_user.username)
        # Add is_system flag for system agents (deletion-protected)
        agent_dict["is_system"] = owner.get("is_system", False) if owner else False

        # Add GitHub repo info if agent was created from GitHub template
        git_config = db.get_git_config(agent_name)
        if git_config:
            agent_dict["github_repo"] = git_config.github_repo
        else:
            agent_dict["github_repo"] = None

        accessible_agents.append(agent_dict)

    return accessible_agents


@router.get("")
async def list_agents_endpoint(request: Request, current_user: User = Depends(get_current_user)):
    """List all agents accessible to the current user."""
    await log_audit_event(
        event_type="agent_access",
        action="list",
        user_id=current_user.username,
        ip_address=request.client.host if request.client else None,
        result="success"
    )

    return get_accessible_agents(current_user)


@router.get("/context-stats")
async def get_agents_context_stats(current_user: User = Depends(get_current_user)):
    """
    Get context window stats and activity state for all accessible agents.

    Returns: List of agent stats with context usage and active/idle/offline state
    """
    import httpx
    from datetime import timedelta

    # Get all accessible agents
    accessible_agents = get_accessible_agents(current_user)
    agent_stats = []

    for agent in accessible_agents:
        agent_name = agent["name"]
        status = agent["status"]

        # Initialize default stats
        stats = {
            "name": agent_name,
            "status": status,  # Docker status: running/stopped/etc
            "activityState": "offline",  # Activity state: active/idle/offline
            "contextPercent": 0,
            "contextUsed": 0,
            "contextMax": 200000,
            "lastActivityTime": None
        }

        # Only fetch context stats for running agents
        if status == "running":
            try:
                # Get agent container
                container = get_agent_container(agent_name)
                if container:
                    # Fetch context stats from agent's internal API
                    agent_url = f"http://{container.name}:8000/api/chat/session"
                    async with httpx.AsyncClient(timeout=2.0) as client:
                        response = await client.get(agent_url)
                        if response.status_code == 200:
                            session_data = response.json()
                            stats["contextPercent"] = session_data.get("context_percent", 0)
                            stats["contextUsed"] = session_data.get("context_tokens", 0)
                            stats["contextMax"] = session_data.get("context_window", 200000)
            except Exception as e:
                # Log error but continue with default values
                print(f"Error fetching context stats for {agent_name}: {e}")

            # Determine active/idle state based on recent activity
            try:
                # Look for any activity in last 60 seconds
                cutoff_time = (datetime.utcnow() - timedelta(seconds=60)).isoformat()
                recent_activities = db.get_agent_activities(
                    agent_name=agent_name,
                    limit=1
                )

                if recent_activities and len(recent_activities) > 0:
                    last_activity = recent_activities[0]
                    activity_time = last_activity.get("created_at")
                    stats["lastActivityTime"] = activity_time

                    # Check if activity is within last 60 seconds
                    if activity_time and activity_time > cutoff_time:
                        # Check if activity is currently running
                        if last_activity.get("activity_state") == "started":
                            stats["activityState"] = "active"
                        else:
                            stats["activityState"] = "idle"
                    else:
                        stats["activityState"] = "idle"
                else:
                    stats["activityState"] = "idle"
            except Exception as e:
                # Default to idle if we can't determine activity
                print(f"Error determining activity state for {agent_name}: {e}")
                stats["activityState"] = "idle"

        agent_stats.append(stats)

    return {"agents": agent_stats}
@router.get("/{agent_name}")
async def get_agent_endpoint(agent_name: str, request: Request, current_user: User = Depends(get_current_user)):
    """Get details of a specific agent."""
    agent = get_agent_by_name(agent_name)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    await log_audit_event(
        event_type="agent_access",
        action="get",
        user_id=current_user.username,
        agent_name=agent_name,
        ip_address=request.client.host if request.client else None,
        result="success"
    )

    agent_dict = agent.dict() if hasattr(agent, 'dict') else dict(agent)
    user_data = db.get_user_by_username(current_user.username)
    is_admin = user_data and user_data["role"] == "admin"

    owner = db.get_agent_owner(agent_name)
    agent_dict["owner"] = owner["owner_username"] if owner else None
    agent_dict["is_owner"] = owner and owner["owner_username"] == current_user.username
    agent_dict["is_shared"] = not agent_dict["is_owner"] and not is_admin and \
                               db.is_agent_shared_with_user(agent_name, current_user.username)
    # Add is_system flag for system agents (deletion-protected)
    agent_dict["is_system"] = owner.get("is_system", False) if owner else False
    agent_dict["can_share"] = db.can_user_share_agent(current_user.username, agent_name)
    agent_dict["can_delete"] = db.can_user_delete_agent(current_user.username, agent_name)

    if agent_dict["can_share"]:
        shares = db.get_agent_shares(agent_name)
        agent_dict["shares"] = [s.dict() for s in shares]
    else:
        agent_dict["shares"] = []

    return agent_dict


async def create_agent_internal(
    config: AgentConfig,
    current_user: User,
    request: Request,
    skip_name_sanitization: bool = False
) -> AgentStatus:
    """
    Internal function to create an agent.

    Used by both the API endpoint and system deployment.

    Args:
        config: Agent configuration
        current_user: Authenticated user
        request: FastAPI request object
        skip_name_sanitization: If True, don't sanitize the name (used when name is pre-validated)

    Returns:
        AgentStatus of the created agent

    Raises:
        HTTPException: On validation or creation errors
    """
    original_name = config.name
    if not skip_name_sanitization:
        config.name = sanitize_agent_name(config.name)

    if not config.name:
        raise HTTPException(status_code=400, detail="Invalid agent name - must contain at least one alphanumeric character")

    if get_agent_by_name(config.name):
        raise HTTPException(status_code=400, detail="Agent already exists")

    template_data = {}
    github_template_path = None
    github_repo_for_agent = None
    github_pat_for_agent = None

    # Load template configuration
    if config.template:
        if config.template.startswith("github:"):
            gh_template = get_github_template(config.template)
            if not gh_template:
                raise HTTPException(status_code=400, detail=f"Unknown GitHub template: {config.template}")

            github_repo = gh_template["github_repo"]
            github_cred_id = gh_template["github_credential_id"]

            github_cred = credential_manager.get_credential(github_cred_id, "admin")
            if not github_cred:
                raise HTTPException(status_code=500, detail="GitHub credential not found in credential store")

            github_secret = credential_manager.get_credential_secret(github_cred_id, "admin")
            if not github_secret:
                raise HTTPException(status_code=500, detail="GitHub credential secret not found")

            github_pat = github_secret.get("token") or github_secret.get("api_key")
            if not github_pat:
                raise HTTPException(status_code=500, detail="GitHub PAT not found in credential")

            github_repo_for_agent = github_repo
            github_pat_for_agent = github_pat
            config.resources = gh_template.get("resources", config.resources)
            config.mcp_servers = gh_template.get("mcp_servers", config.mcp_servers)

            # Generate git sync instance ID and branch for Phase 7
            git_instance_id = git_service.generate_instance_id()
            git_working_branch = git_service.generate_working_branch(config.name, git_instance_id)
        elif config.template.startswith("local:"):
            # Local template - strip "local:" prefix
            template_name = config.template[6:]  # Remove "local:" prefix
            templates_dir = Path("/agent-configs/templates")
            if not templates_dir.exists():
                templates_dir = Path("./config/agent-templates")

            template_path = templates_dir / template_name
            template_yaml = template_path / "template.yaml"

            if template_yaml.exists():
                try:
                    with open(template_yaml) as f:
                        template_data = yaml.safe_load(f)
                        config.type = template_data.get("type", config.type)
                        config.resources = template_data.get("resources", config.resources)
                        config.tools = template_data.get("tools", config.tools)
                        creds = template_data.get("credentials", {})
                        mcp_servers = list(creds.get("mcp_servers", {}).keys())
                        if mcp_servers:
                            config.mcp_servers = mcp_servers
                except Exception as e:
                    print(f"Error loading template config: {e}")

    if config.port is None:
        config.port = get_next_available_port()

    agent_credentials = credential_manager.get_agent_credentials(config.name, config.mcp_servers, current_user.username)

    flat_credentials = {}
    for server, creds in agent_credentials.items():
        if isinstance(creds, dict):
            for key, value in creds.items():
                flat_credentials[key.upper()] = value
        else:
            flat_credentials[server.upper()] = creds

    generated_files = {}
    if template_data:
        generated_files = generate_credential_files(
            template_data, flat_credentials, config.name,
            template_base_path=github_template_path
        )

    cred_files_dir = Path(f"/tmp/agent-{config.name}-creds")
    cred_files_dir.mkdir(exist_ok=True)
    for filepath, content in generated_files.items():
        file_path = cred_files_dir / filepath
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w") as f:
            f.write(content)

    agent_config = {
        "agent": {
            "type": config.type,
            "base_image": config.base_image,
            "resources": config.resources,
            "tools": config.tools,
            "mcp_servers": config.mcp_servers,
            "custom_instructions": config.custom_instructions,
            "credentials": {server: "injected" for server in config.mcp_servers}
        }
    }

    config_path = Path(f"/tmp/agent-{config.name}.yaml")
    with open(config_path, "w") as f:
        yaml.dump(agent_config, f)

    credentials_path = Path(f"/tmp/agent-{config.name}-credentials.json")
    with open(credentials_path, "w") as f:
        json.dump(agent_credentials, f)

    template_volume = None
    cred_files_volume = None
    if config.template:
        if config.template.startswith("github:"):
            pass  # Agent clones at startup
        elif config.template.startswith("local:"):
            # Local template - strip "local:" prefix for path resolution
            template_name = config.template[6:]  # Remove "local:" prefix
            templates_dir = Path("/agent-configs/templates")
            template_path_in_backend = templates_dir / template_name

            if template_path_in_backend.exists():
                host_templates_base = os.getenv("HOST_TEMPLATES_PATH", "./config/agent-templates")
                host_template_path = Path(host_templates_base) / template_name
                template_volume = {str(host_template_path): {'bind': '/template', 'mode': 'ro'}}

        if generated_files:
            cred_files_volume = {str(cred_files_dir): {'bind': '/generated-creds', 'mode': 'ro'}}

    # Phase: Agent-to-Agent Collaboration
    # Generate agent-scoped MCP API key for Trinity MCP access
    agent_mcp_key = None
    trinity_mcp_url = os.getenv('TRINITY_MCP_URL', 'http://mcp-server:8080/mcp')
    try:
        agent_mcp_key = db.create_agent_mcp_api_key(
            agent_name=config.name,
            owner_username=current_user.username,
            description=f"Auto-generated Trinity MCP key for agent {config.name}"
        )
        if agent_mcp_key:
            print(f"Created MCP API key for agent {config.name}: {agent_mcp_key.key_prefix}...")
    except Exception as e:
        print(f"Warning: Failed to create MCP API key for agent {config.name}: {e}")

    env_vars = {
        'AGENT_NAME': config.name,
        'AGENT_TYPE': config.type,
        'CREDENTIALS_FILE': '/config/credentials.json',
        'ANTHROPIC_API_KEY': get_anthropic_api_key(),
        'ENABLE_SSH': 'true',
        'ENABLE_AGENT_UI': 'true',
        'AGENT_SERVER_PORT': '8000',
        'TEMPLATE_NAME': config.template if config.template else ''
    }

    # OpenTelemetry Configuration (opt-in via OTEL_ENABLED)
    # Claude Code has built-in OTel support - these vars enable metrics export
    if os.getenv('OTEL_ENABLED', '0') == '1':
        env_vars['CLAUDE_CODE_ENABLE_TELEMETRY'] = '1'
        env_vars['OTEL_METRICS_EXPORTER'] = os.getenv('OTEL_METRICS_EXPORTER', 'otlp')
        env_vars['OTEL_LOGS_EXPORTER'] = os.getenv('OTEL_LOGS_EXPORTER', 'otlp')
        env_vars['OTEL_EXPORTER_OTLP_PROTOCOL'] = os.getenv('OTEL_EXPORTER_OTLP_PROTOCOL', 'grpc')
        env_vars['OTEL_EXPORTER_OTLP_ENDPOINT'] = os.getenv('OTEL_COLLECTOR_ENDPOINT', 'http://trinity-otel-collector:4317')
        env_vars['OTEL_METRIC_EXPORT_INTERVAL'] = os.getenv('OTEL_METRIC_EXPORT_INTERVAL', '60000')

    # Phase: Agent-to-Agent Collaboration - Inject Trinity MCP credentials
    if agent_mcp_key:
        env_vars['TRINITY_MCP_URL'] = trinity_mcp_url
        env_vars['TRINITY_MCP_API_KEY'] = agent_mcp_key.api_key

    if github_repo_for_agent and github_pat_for_agent:
        env_vars['GITHUB_REPO'] = github_repo_for_agent
        env_vars['GITHUB_PAT'] = github_pat_for_agent
        # Phase 7: Enable git sync for GitHub-native agents
        env_vars['GIT_SYNC_ENABLED'] = 'true'
        env_vars['GIT_WORKING_BRANCH'] = git_working_branch

    for server, creds in agent_credentials.items():
        server_upper = server.upper().replace('-', '_')
        if 'api_key' in creds:
            env_vars[f'{server_upper}_API_KEY'] = creds['api_key']
        if 'token' in creds:
            env_vars[f'{server_upper}_TOKEN'] = creds['token']
        if 'access_token' in creds:
            env_vars[f'{server_upper}_ACCESS_TOKEN'] = creds['access_token']
        if 'refresh_token' in creds:
            env_vars[f'{server_upper}_REFRESH_TOKEN'] = creds['refresh_token']
        if 'username' in creds and 'password' in creds:
            env_vars[f'{server_upper}_USERNAME'] = creds['username']
            env_vars[f'{server_upper}_PASSWORD'] = creds['password']

    if docker_client:
        try:
            # Create per-agent persistent volume for /home/developer (Pillar III: Persistent Memory)
            # This ensures files created by the agent survive container restarts
            agent_volume_name = f"agent-{config.name}-workspace"
            try:
                docker_client.volumes.get(agent_volume_name)
            except docker.errors.NotFound:
                docker_client.volumes.create(
                    name=agent_volume_name,
                    labels={
                        'trinity.platform': 'agent-workspace',
                        'trinity.agent-name': config.name
                    }
                )

            volumes = {
                str(config_path): {'bind': '/config/agent-config.yaml', 'mode': 'ro'},
                str(credentials_path): {'bind': '/config/credentials.json', 'mode': 'ro'},
                'encrypted-data': {'bind': '/data', 'mode': 'rw'},
                'audit-logs': {'bind': '/logs', 'mode': 'rw'},
                agent_volume_name: {'bind': '/home/developer', 'mode': 'rw'}  # Persistent workspace
            }

            if template_volume:
                volumes.update(template_volume)
            if cred_files_volume:
                volumes.update(cred_files_volume)

            # Phase 9: Mount Trinity meta-prompt for agent collaboration
            # Check if meta-prompt exists in container, but use HOST path for agent volume mount
            container_meta_prompt_path = Path("/trinity-meta-prompt")
            host_meta_prompt_path = os.getenv("HOST_META_PROMPT_PATH", "./config/trinity-meta-prompt")
            if container_meta_prompt_path.exists():
                volumes[host_meta_prompt_path] = {'bind': '/trinity-meta-prompt', 'mode': 'ro'}

            # Phase 9.11: Agent Shared Folders - mount shared volumes based on config
            shared_folder_config = db.get_shared_folder_config(config.name)
            if shared_folder_config:
                # If agent exposes a shared folder, create and mount the shared volume
                if shared_folder_config.expose_enabled:
                    shared_volume_name = db.get_shared_volume_name(config.name)
                    volume_created = False
                    try:
                        docker_client.volumes.get(shared_volume_name)
                    except docker.errors.NotFound:
                        docker_client.volumes.create(
                            name=shared_volume_name,
                            labels={
                                'trinity.platform': 'agent-shared',
                                'trinity.agent-name': config.name
                            }
                        )
                        volume_created = True

                    # Fix ownership of new volumes (Docker creates them as root)
                    if volume_created:
                        try:
                            docker_client.containers.run(
                                'alpine',
                                command='chown 1000:1000 /shared',
                                volumes={shared_volume_name: {'bind': '/shared', 'mode': 'rw'}},
                                remove=True
                            )
                        except Exception as e:
                            logger.warning(f"Could not fix shared volume ownership: {e}")

                    volumes[shared_volume_name] = {'bind': '/home/developer/shared-out', 'mode': 'rw'}

                # If agent consumes shared folders, mount available shared volumes
                if shared_folder_config.consume_enabled:
                    available_folders = db.get_available_shared_folders(config.name)
                    for source_agent in available_folders:
                        source_volume = db.get_shared_volume_name(source_agent)
                        mount_path = db.get_shared_mount_path(source_agent)
                        # Only mount if the source volume exists
                        try:
                            docker_client.volumes.get(source_volume)
                            volumes[source_volume] = {'bind': mount_path, 'mode': 'rw'}
                        except docker.errors.NotFound:
                            # Source agent hasn't started yet or doesn't have shared volume
                            pass

            container = docker_client.containers.run(
                config.base_image,
                detach=True,
                name=f"agent-{config.name}",
                ports={'22/tcp': config.port},
                volumes=volumes,
                environment=env_vars,
                labels={
                    'trinity.platform': 'agent',
                    'trinity.agent-name': config.name,
                    'trinity.agent-type': config.type,
                    'trinity.ssh-port': str(config.port),
                    'trinity.cpu': config.resources['cpu'],
                    'trinity.memory': config.resources['memory'],
                    'trinity.created': datetime.now().isoformat(),
                    'trinity.template': config.template or ''
                },
                security_opt=['no-new-privileges:true', 'apparmor:docker-default'],
                cap_drop=['ALL'],
                cap_add=['NET_BIND_SERVICE'],
                read_only=False,
                tmpfs={'/tmp': 'noexec,nosuid,size=100m'},
                network='trinity-agent-network',
                mem_limit=config.resources.get('memory', '4Gi'),
                cpu_count=int(config.resources.get('cpu', '2'))
            )

            agent_status = get_agent_status_from_container(container)

            if manager:
                await manager.broadcast(json.dumps({
                    "event": "agent_created",
                    "data": {
                        "name": agent_status.name,
                        "type": agent_status.type,
                        "status": agent_status.status,
                        "port": agent_status.port,
                        "created": agent_status.created.isoformat(),
                        "resources": agent_status.resources,
                        "container_id": agent_status.container_id
                    }
                }))

            db.register_agent_owner(config.name, current_user.username)

            # Phase 9.10: Grant default permissions (Option B - same-owner agents)
            try:
                permissions_count = db.grant_default_permissions(config.name, current_user.username)
                if permissions_count > 0:
                    print(f"Granted {permissions_count} default permissions for agent {config.name}")
            except Exception as e:
                print(f"Warning: Failed to grant default permissions for {config.name}: {e}")

            # Phase 7: Create git config for GitHub-native agents
            if github_repo_for_agent:
                try:
                    db.create_git_config(
                        agent_name=config.name,
                        github_repo=github_repo_for_agent,
                        working_branch=git_working_branch,
                        instance_id=git_instance_id
                    )
                except Exception as e:
                    print(f"Warning: Failed to create git config for {config.name}: {e}")

            await log_audit_event(
                event_type="agent_management",
                action="create",
                user_id=current_user.username,
                agent_name=config.name,
                resource=f"agent-{config.name}",
                ip_address=request.client.host if request.client else None,
                result="success",
                details={
                    "type": config.type,
                    "port": config.port,
                    "owner": current_user.username,
                    "git_sync": bool(github_repo_for_agent),
                    "git_branch": git_working_branch if github_repo_for_agent else None
                }
            )

            return agent_status
        except Exception as e:
            await log_audit_event(
                event_type="agent_management",
                action="create",
                user_id=current_user.username,
                agent_name=config.name,
                resource=f"agent-{config.name}",
                ip_address=request.client.host if request.client else None,
                result="failed",
                severity="error",
                details={"error_type": type(e).__name__}  # Don't expose error message
            )
            import logging
            logging.getLogger("trinity.errors").error(f"Failed to create agent {config.name}: {e}")
            raise HTTPException(status_code=500, detail="Failed to create agent. Please try again.")
    else:
        raise HTTPException(
            status_code=503,
            detail="Docker not available - cannot create agents in demo mode"
        )


@router.post("")
async def create_agent_endpoint(config: AgentConfig, request: Request, current_user: User = Depends(get_current_user)):
    """Create a new agent."""
    return await create_agent_internal(config, current_user, request, skip_name_sanitization=False)


# ============================================================================
# Local Agent Deployment (Deploy from local directory via MCP)
# ============================================================================

import base64
import tarfile
import tempfile
import shutil
from io import BytesIO
from typing import List

from models import DeployLocalRequest, DeployLocalResponse, VersioningInfo, CredentialImportResult
from services.template_service import is_trinity_compatible

# Size limits for local deployment
MAX_ARCHIVE_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_CREDENTIALS = 100
MAX_FILES = 1000


def get_agents_by_prefix(prefix: str) -> List[AgentStatus]:
    """
    Get all agents that match a base name prefix.

    Matches:
    - Exact name (e.g., "my-agent")
    - Versioned names (e.g., "my-agent-2", "my-agent-3")

    Args:
        prefix: Base agent name to match

    Returns:
        List of matching AgentStatus objects
    """
    all_agents = list_all_agents()
    matching = []

    for agent in all_agents:
        name = agent.name
        if name == prefix:
            matching.append(agent)
        elif name.startswith(f"{prefix}-"):
            # Check if suffix is a version number
            suffix = name[len(prefix) + 1:]
            if suffix.isdigit():
                matching.append(agent)

    return matching


def get_next_version_name(base_name: str) -> str:
    """
    Get next available version name for an agent.

    Pattern: {base-name} -> {base-name}-2 -> {base-name}-3

    Args:
        base_name: Base agent name

    Returns:
        Next available version name
    """
    existing = get_agents_by_prefix(base_name)

    if not existing:
        return base_name

    # Find highest version number
    max_version = 1
    for agent in existing:
        if agent.name == base_name:
            max_version = max(max_version, 1)
        elif agent.name.startswith(f"{base_name}-"):
            suffix = agent.name[len(base_name) + 1:]
            try:
                v = int(suffix)
                max_version = max(max_version, v)
            except ValueError:
                pass

    return f"{base_name}-{max_version + 1}"


def get_latest_version(base_name: str) -> Optional[AgentStatus]:
    """
    Get the most recent version of an agent.

    Args:
        base_name: Base agent name

    Returns:
        Most recent AgentStatus or None if no versions exist
    """
    existing = get_agents_by_prefix(base_name)
    if not existing:
        return None

    # Sort by version number (highest first)
    def get_version(agent):
        if agent.name == base_name:
            return 1
        suffix = agent.name[len(base_name) + 1:]
        try:
            return int(suffix)
        except ValueError:
            return 0

    return max(existing, key=get_version)


@router.post("/deploy-local")
async def deploy_local_agent(
    body: DeployLocalRequest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Deploy a Trinity-compatible local agent.

    This endpoint receives a base64-encoded tar.gz archive of a local agent
    directory, validates it's Trinity-compatible (has template.yaml), handles
    versioning if an agent with the same name exists, imports credentials,
    and creates the agent.

    Flow:
    1. Decode and extract base64 archive
    2. Validate Trinity-compatible (template.yaml exists)
    3. Handle versioning (stop old version if exists)
    4. Import credentials with conflict resolution
    5. Copy to templates directory
    6. Create agent via create_agent_internal
    7. Hot-reload credentials into running agent
    8. Return response with version and credential info
    """
    import httpx

    temp_dir = None

    try:
        # 1. Validate archive size
        try:
            archive_bytes = base64.b64decode(body.archive)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Invalid base64 encoding: {e}",
                    "code": "INVALID_ARCHIVE"
                }
            )

        if len(archive_bytes) > MAX_ARCHIVE_SIZE:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Archive exceeds maximum size of {MAX_ARCHIVE_SIZE // (1024*1024)}MB",
                    "code": "ARCHIVE_TOO_LARGE"
                }
            )

        # 2. Validate credentials count
        if body.credentials and len(body.credentials) > MAX_CREDENTIALS:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Credentials exceed maximum count of {MAX_CREDENTIALS}",
                    "code": "TOO_MANY_CREDENTIALS"
                }
            )

        # 3. Extract archive to temp directory
        temp_dir = Path(tempfile.mkdtemp(prefix="trinity-deploy-"))
        try:
            with tarfile.open(fileobj=BytesIO(archive_bytes), mode='r:gz') as tar:
                # Security: Check for path traversal
                for member in tar.getmembers():
                    if member.name.startswith('/') or '..' in member.name:
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "error": "Invalid archive: contains path traversal",
                                "code": "INVALID_ARCHIVE"
                            }
                        )

                # Check file count
                if len(tar.getmembers()) > MAX_FILES:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": f"Archive exceeds maximum file count of {MAX_FILES}",
                            "code": "TOO_MANY_FILES"
                        }
                    )

                tar.extractall(temp_dir)
        except tarfile.TarError as e:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Invalid tar.gz archive: {e}",
                    "code": "INVALID_ARCHIVE"
                }
            )

        # 4. Find the root directory (handle nested extraction)
        contents = list(temp_dir.iterdir())
        if len(contents) == 1 and contents[0].is_dir():
            extract_root = contents[0]
        else:
            extract_root = temp_dir

        # 5. Validate Trinity-compatible
        is_compatible, error_msg, template_data = is_trinity_compatible(extract_root)
        if not is_compatible:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Agent is not Trinity-compatible: {error_msg}",
                    "code": "NOT_TRINITY_COMPATIBLE"
                }
            )

        # 6. Determine agent name
        base_name = body.name or template_data.get("name")
        if not base_name:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "No agent name specified and template.yaml has no name field",
                    "code": "MISSING_NAME"
                }
            )

        base_name = sanitize_agent_name(base_name)

        # 7. Version handling
        version_name = get_next_version_name(base_name)
        previous_version = get_latest_version(base_name)
        previous_stopped = False

        if previous_version and previous_version.status == "running":
            # Stop the previous version
            try:
                container = get_agent_container(previous_version.name)
                if container:
                    container.stop()
                    previous_stopped = True
                    print(f"Stopped previous version: {previous_version.name}")
            except Exception as e:
                print(f"Warning: Failed to stop previous version {previous_version.name}: {e}")

        # 8. Import credentials
        cred_results = {}
        if body.credentials:
            for key, value in body.credentials.items():
                result = credential_manager.import_credential_with_conflict_resolution(
                    key, value, current_user.username
                )
                cred_results[key] = CredentialImportResult(
                    status=result["status"],
                    name=result["name"],
                    original=result.get("original")
                )

        # 9. Copy to templates directory
        # Try /agent-configs/templates first, but check if writable (not just if exists)
        # The read-only mount makes this path exist but not writable
        templates_dir = Path("/agent-configs/templates")

        # Check if writable by attempting to create a test file
        try:
            test_file = templates_dir / ".write_test"
            test_file.touch()
            test_file.unlink()
        except (OSError, PermissionError):
            # Fall back to local config path
            templates_dir = Path("./config/agent-templates")

        if not templates_dir.exists():
            templates_dir.mkdir(parents=True, exist_ok=True)

        dest_path = templates_dir / version_name
        if dest_path.exists():
            shutil.rmtree(dest_path)

        shutil.copytree(extract_root, dest_path)
        print(f"Copied agent template to: {dest_path}")

        # 10. Create agent
        agent_config = AgentConfig(
            name=version_name,
            template=f"local:{version_name}",
            type=template_data.get("type", "business-assistant"),
            resources=template_data.get("resources", {"cpu": "2", "memory": "4g"})
        )

        agent_status = await create_agent_internal(
            agent_config,
            current_user,
            request,
            skip_name_sanitization=True
        )

        # 11. Hot-reload credentials if any were imported
        if body.credentials:
            try:
                # Wait a moment for the agent to start
                import asyncio
                await asyncio.sleep(2)

                async with httpx.AsyncClient(timeout=30.0) as client:
                    await client.post(
                        f"http://agent-{version_name}:8000/api/credentials/update",
                        json={
                            "credentials": {k: v for k, v in body.credentials.items()},
                            "mcp_config": None
                        }
                    )
                    print(f"Hot-reloaded {len(body.credentials)} credentials into {version_name}")
            except Exception as e:
                print(f"Warning: Failed to hot-reload credentials: {e}")

        # 12. Audit log
        await log_audit_event(
            event_type="agent_management",
            action="deploy_local",
            user_id=current_user.username,
            agent_name=version_name,
            ip_address=request.client.host if request.client else None,
            result="success",
            details={
                "base_name": base_name,
                "previous_version": previous_version.name if previous_version else None,
                "previous_stopped": previous_stopped,
                "credentials_count": len(cred_results),
                "archive_size": len(archive_bytes)
            }
        )

        # 13. Return response
        return DeployLocalResponse(
            status="success",
            agent=agent_status,
            versioning=VersioningInfo(
                base_name=base_name,
                previous_version=previous_version.name if previous_version else None,
                previous_version_stopped=previous_stopped,
                new_version=version_name
            ),
            credentials_imported={k: v.dict() for k, v in cred_results.items()},
            credentials_injected=len(cred_results)
        )

    except HTTPException:
        raise
    except Exception as e:
        await log_audit_event(
            event_type="agent_management",
            action="deploy_local",
            user_id=current_user.username,
            ip_address=request.client.host if request.client else None,
            result="failed",
            severity="error",
            details={"error": str(e)}
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to deploy local agent: {str(e)}"
        )
    finally:
        # Cleanup temp directory
        if temp_dir and temp_dir.exists():
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass


@router.delete("/{agent_name}")
async def delete_agent_endpoint(agent_name: str, request: Request, current_user: User = Depends(get_current_user)):
    """Delete an agent.

    Note: System agents (like trinity-system) cannot be deleted by anyone.
    They can only be re-initialized by admins.
    """
    # Check for system agent first - no one can delete these
    if db.is_system_agent(agent_name):
        await log_audit_event(
            event_type="agent_management",
            action="delete",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="forbidden",
            severity="warning",
            details={"reason": "system_agent_protected"}
        )
        raise HTTPException(
            status_code=403,
            detail="System agents cannot be deleted. Use re-initialization to reset to clean state."
        )

    if not db.can_user_delete_agent(current_user.username, agent_name):
        await log_audit_event(
            event_type="agent_management",
            action="delete",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="unauthorized",
            severity="warning"
        )
        raise HTTPException(status_code=403, detail="You don't have permission to delete this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        container.stop()
        container.remove()
    except Exception as e:
        print(f"Error stopping/removing container: {e}")

    # Delete per-agent persistent volume (Pillar III: Persistent Memory cleanup)
    try:
        agent_volume_name = f"agent-{agent_name}-workspace"
        volume = docker_client.volumes.get(agent_volume_name)
        volume.remove()
    except docker.errors.NotFound:
        pass  # Volume doesn't exist
    except Exception as e:
        print(f"Warning: Failed to delete workspace volume for agent {agent_name}: {e}")

    # Delete all schedules for this agent (also removes from scheduler)
    schedules = db.list_agent_schedules(agent_name)
    for schedule in schedules:
        scheduler_service.remove_schedule(schedule.id)
    db.delete_agent_schedules(agent_name)

    # Phase 7: Delete git config if exists
    git_service.delete_agent_git_config(agent_name)

    # Phase: Agent-to-Agent Collaboration - Delete agent's MCP API key
    try:
        db.delete_agent_mcp_api_key(agent_name)
    except Exception as e:
        print(f"Warning: Failed to delete MCP API key for agent {agent_name}: {e}")

    # Phase 9.10: Delete agent permissions (both as source and target)
    try:
        db.delete_agent_permissions(agent_name)
    except Exception as e:
        print(f"Warning: Failed to delete permissions for agent {agent_name}: {e}")

    # Phase 9.11: Delete shared folder config and shared volume
    try:
        db.delete_shared_folder_config(agent_name)
        # Also delete the shared volume if it exists
        shared_volume_name = db.get_shared_volume_name(agent_name)
        try:
            shared_volume = docker_client.volumes.get(shared_volume_name)
            shared_volume.remove()
        except docker.errors.NotFound:
            pass  # Volume doesn't exist
    except Exception as e:
        print(f"Warning: Failed to delete shared folder config for agent {agent_name}: {e}")

    db.delete_agent_ownership(agent_name)

    if manager:
        await manager.broadcast(json.dumps({
            "event": "agent_deleted",
            "data": {"name": agent_name}
        }))

    await log_audit_event(
        event_type="agent_management",
        action="delete",
        user_id=current_user.username,
        agent_name=agent_name,
        ip_address=request.client.host if request.client else None,
        result="success"
    )

    return {"message": f"Agent {agent_name} deleted"}


async def _recreate_container_with_updated_config(agent_name: str, old_container, owner_username: str):
    """
    Recreate an agent container with updated configuration.
    Handles shared folder mounts and API key settings.
    Preserves the agent's workspace volume and other configuration.
    """
    # Extract configuration from old container
    old_config = old_container.attrs.get("Config", {})
    old_host_config = old_container.attrs.get("HostConfig", {})

    # Get key settings
    image = old_config.get("Image", "trinity-agent:latest")
    env_vars = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in old_config.get("Env", []) if "=" in e}
    labels = old_config.get("Labels", {})

    # Update ANTHROPIC_API_KEY based on current setting
    use_platform_key = db.get_use_platform_api_key(agent_name)
    if use_platform_key:
        # Use platform API key
        env_vars['ANTHROPIC_API_KEY'] = get_anthropic_api_key()
    else:
        # Remove platform key - user will auth in terminal
        env_vars.pop('ANTHROPIC_API_KEY', None)

    # Get port from labels
    ssh_port = int(labels.get("trinity.ssh-port", 2222))

    # Get resource limits
    cpu = labels.get("trinity.cpu", "2")
    memory = labels.get("trinity.memory", "4g")

    # Stop and remove old container
    try:
        old_container.stop()
    except Exception:
        pass
    old_container.remove()

    # Build new volume configuration
    agent_volume_name = f"agent-{agent_name}-workspace"

    # Start with base volumes - get existing bind mounts
    old_mounts = old_container.attrs.get("Mounts", [])
    volumes = {}

    for m in old_mounts:
        dest = m.get("Destination", "")
        # Skip shared folder mounts - we'll add the correct ones
        if dest == "/home/developer/shared-out" or dest.startswith("/home/developer/shared-in/"):
            continue
        # Keep other mounts
        if m.get("Type") == "bind":
            volumes[m.get("Source")] = {"bind": dest, "mode": "rw" if m.get("RW", True) else "ro"}
        elif m.get("Type") == "volume":
            vol_name = m.get("Name")
            if vol_name:
                volumes[vol_name] = {"bind": dest, "mode": "rw" if m.get("RW", True) else "ro"}

    # Add shared folder mounts based on current config
    shared_config = db.get_shared_folder_config(agent_name)
    if shared_config:
        if shared_config.expose_enabled:
            shared_volume_name = db.get_shared_volume_name(agent_name)
            volume_created = False
            try:
                docker_client.volumes.get(shared_volume_name)
            except docker.errors.NotFound:
                docker_client.volumes.create(
                    name=shared_volume_name,
                    labels={
                        'trinity.platform': 'agent-shared',
                        'trinity.agent-name': agent_name
                    }
                )
                volume_created = True

            # Fix ownership of new volumes (Docker creates them as root)
            if volume_created:
                try:
                    docker_client.containers.run(
                        'alpine',
                        command='chown 1000:1000 /shared',
                        volumes={shared_volume_name: {'bind': '/shared', 'mode': 'rw'}},
                        remove=True
                    )
                except Exception as e:
                    logger.warning(f"Could not fix shared volume ownership: {e}")

            volumes[shared_volume_name] = {'bind': '/home/developer/shared-out', 'mode': 'rw'}

        if shared_config.consume_enabled:
            available_folders = db.get_available_shared_folders(agent_name)
            for source_agent in available_folders:
                source_volume = db.get_shared_volume_name(source_agent)
                mount_path = db.get_shared_mount_path(source_agent)
                try:
                    docker_client.volumes.get(source_volume)
                    volumes[source_volume] = {'bind': mount_path, 'mode': 'rw'}
                except docker.errors.NotFound:
                    pass

    # Create new container
    new_container = docker_client.containers.run(
        image,
        detach=True,
        name=f"agent-{agent_name}",
        ports={'22/tcp': ssh_port},
        volumes=volumes,
        environment=env_vars,
        labels=labels,
        security_opt=['no-new-privileges:true', 'apparmor:docker-default'],
        cap_drop=['ALL'],
        cap_add=['NET_BIND_SERVICE'],
        read_only=False,
        tmpfs={'/tmp': 'noexec,nosuid,size=100m'},
        network='trinity-agent-network',
        mem_limit=memory,
        cpu_count=int(cpu)
    )

    print(f"Recreated container for agent {agent_name} with updated configuration")
    return new_container


def _check_shared_folder_mounts_match(container, agent_name: str) -> bool:
    """
    Check if container's shared folder mounts match the current config.
    Returns True if mounts are correct, False if recreation needed.
    """
    config = db.get_shared_folder_config(agent_name)
    if not config:
        # No config - check that no shared mounts exist
        mounts = container.attrs.get("Mounts", [])
        for m in mounts:
            dest = m.get("Destination", "")
            if dest == "/home/developer/shared-out" or dest.startswith("/home/developer/shared-in/"):
                return False  # Has mounts but no config - needs recreation to remove
        return True

    mounts = container.attrs.get("Mounts", [])
    mount_dests = {m.get("Destination") for m in mounts}

    # Check expose mount
    if config.expose_enabled:
        if "/home/developer/shared-out" not in mount_dests:
            return False  # Config says expose, but mount missing
    else:
        if "/home/developer/shared-out" in mount_dests:
            return False  # Config says no expose, but mount exists

    # Check consume mounts
    if config.consume_enabled:
        available = db.get_available_shared_folders(agent_name)
        for source_agent in available:
            mount_path = db.get_shared_mount_path(source_agent)
            source_volume = db.get_shared_volume_name(source_agent)
            # Check if volume exists
            try:
                docker_client.volumes.get(source_volume)
                if mount_path not in mount_dests:
                    return False  # Should be mounted but isn't
            except docker.errors.NotFound:
                pass  # Volume doesn't exist yet, OK to skip

    return True


def _check_api_key_env_matches(container, agent_name: str) -> bool:
    """
    Check if container's ANTHROPIC_API_KEY env var matches the current setting.
    Returns True if env matches config, False if recreation needed.
    """
    use_platform_key = db.get_use_platform_api_key(agent_name)

    # Get current env vars from container
    env_list = container.attrs.get("Config", {}).get("Env", [])
    env_dict = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in env_list if "=" in e}

    has_api_key = "ANTHROPIC_API_KEY" in env_dict and env_dict["ANTHROPIC_API_KEY"]

    if use_platform_key:
        # Should have the platform key - check if it's current
        expected_key = get_anthropic_api_key()
        current_key = env_dict.get("ANTHROPIC_API_KEY", "")
        return current_key == expected_key
    else:
        # Should NOT have the key
        return not has_api_key


@router.post("/{agent_name}/start")
async def start_agent_endpoint(agent_name: str, request: Request, current_user: User = Depends(get_current_user)):
    """Start an agent."""
    try:
        # Use internal function for core start logic
        result = await start_agent_internal(agent_name)
        trinity_status = result.get("trinity_injection", "unknown")

        if manager:
            await manager.broadcast(json.dumps({
                "event": "agent_started",
                "data": {"name": agent_name, "trinity_injection": trinity_status}
            }))

        await log_audit_event(
            event_type="agent_management",
            action="start",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="success",
            details={"trinity_injection": trinity_status}
        )

        return {"message": f"Agent {agent_name} started", "trinity_injection": trinity_status}
    except HTTPException:
        raise
    except Exception as e:
        await log_audit_event(
            event_type="agent_management",
            action="start",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="failed",
            severity="error",
            details={"error": str(e)}
        )
        raise HTTPException(status_code=500, detail=f"Failed to start agent: {str(e)}")


@router.post("/{agent_name}/stop")
async def stop_agent_endpoint(agent_name: str, request: Request, current_user: User = Depends(get_current_user)):
    """Stop an agent."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        container.stop()

        if manager:
            await manager.broadcast(json.dumps({
                "event": "agent_stopped",
                "data": {"name": agent_name}
            }))

        await log_audit_event(
            event_type="agent_management",
            action="stop",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="success"
        )

        return {"message": f"Agent {agent_name} stopped"}
    except Exception as e:
        await log_audit_event(
            event_type="agent_management",
            action="stop",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="failed",
            severity="error",
            details={"error": str(e)}
        )
        raise HTTPException(status_code=500, detail=f"Failed to stop agent: {str(e)}")


@router.get("/{agent_name}/logs")
async def get_agent_logs_endpoint(
    agent_name: str,
    request: Request,
    tail: int = 100,
    current_user: User = Depends(get_current_user)
):
    """Get agent container logs."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        logs = container.logs(tail=tail).decode('utf-8')

        await log_audit_event(
            event_type="agent_access",
            action="view_logs",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="success"
        )

        return {"logs": logs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get logs: {str(e)}")


@router.get("/{agent_name}/stats")
async def get_agent_stats_endpoint(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get live container stats (CPU, memory, network) for an agent."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    container.reload()
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent is not running")

    try:
        stats = container.stats(stream=False)

        cpu_percent = 0.0
        cpu_stats = stats.get("cpu_stats", {})
        precpu_stats = stats.get("precpu_stats", {})

        cpu_delta = cpu_stats.get("cpu_usage", {}).get("total_usage", 0) - \
                    precpu_stats.get("cpu_usage", {}).get("total_usage", 0)
        system_delta = cpu_stats.get("system_cpu_usage", 0) - \
                       precpu_stats.get("system_cpu_usage", 0)

        if system_delta > 0 and cpu_delta > 0:
            num_cpus = len(cpu_stats.get("cpu_usage", {}).get("percpu_usage", [])) or 1
            cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0

        memory_stats = stats.get("memory_stats", {})
        memory_used = memory_stats.get("usage", 0)
        memory_limit = memory_stats.get("limit", 0)
        cache = memory_stats.get("stats", {}).get("cache", 0)
        memory_used_actual = max(0, memory_used - cache)

        networks = stats.get("networks", {})
        network_rx = sum(net.get("rx_bytes", 0) for net in networks.values())
        network_tx = sum(net.get("tx_bytes", 0) for net in networks.values())

        started_at = container.attrs.get("State", {}).get("StartedAt", "")
        uptime_seconds = 0
        if started_at:
            try:
                start_time = datetime.fromisoformat(started_at.replace("Z", "+00:00").split(".")[0])
                uptime_seconds = int((datetime.now(start_time.tzinfo) - start_time).total_seconds())
            except Exception:
                pass

        return {
            "cpu_percent": round(cpu_percent, 1),
            "memory_used_bytes": memory_used_actual,
            "memory_limit_bytes": memory_limit,
            "memory_percent": round((memory_used_actual / memory_limit * 100) if memory_limit > 0 else 0, 1),
            "network_rx_bytes": network_rx,
            "network_tx_bytes": network_tx,
            "uptime_seconds": uptime_seconds,
            "status": container.status
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get stats: {str(e)}")


@router.get("/{agent_name}/queue")
async def get_agent_queue_status(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get execution queue status for an agent.

    Returns:
    - is_busy: Whether the agent is currently executing a request
    - current_execution: Details of the currently running execution (if any)
    - queue_length: Number of requests waiting in the queue
    - queued_executions: Details of queued requests

    This endpoint is useful for checking if an agent is available before
    sending a chat request, or for monitoring agent workload.
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        queue = get_execution_queue()
        status = await queue.get_status(agent_name)
        return status.model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get queue status: {str(e)}")


@router.post("/{agent_name}/queue/clear")
async def clear_agent_queue(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Clear all queued executions for an agent.

    This does NOT stop the currently running execution - only clears pending requests.
    Use this if you want to cancel all waiting requests for an agent.

    Returns the number of cleared executions.
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        queue = get_execution_queue()
        cleared_count = await queue.clear_queue(agent_name)

        await log_audit_event(
            event_type="agent_queue",
            action="clear_queue",
            user_id=current_user.username,
            agent_name=agent_name,
            resource=f"agent-{agent_name}",
            result="success",
            details={"cleared_count": cleared_count}
        )

        return {
            "status": "cleared",
            "agent": agent_name,
            "cleared_count": cleared_count
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear queue: {str(e)}")


@router.post("/{agent_name}/queue/release")
async def force_release_agent(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Force release an agent from its running state.

    CAUTION: This is an emergency operation for when an agent is stuck.
    Use only if an execution is hung or the agent died without completing.

    This clears the "running" state in the queue, allowing new executions.
    It does NOT stop any actual agent process - just resets the queue state.
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        queue = get_execution_queue()
        was_running = await queue.force_release(agent_name)

        await log_audit_event(
            event_type="agent_queue",
            action="force_release",
            user_id=current_user.username,
            agent_name=agent_name,
            resource=f"agent-{agent_name}",
            result="success",
            details={"was_running": was_running},
            severity="warning"
        )

        return {
            "status": "released",
            "agent": agent_name,
            "was_running": was_running,
            "warning": "Agent queue state has been reset. Any in-progress execution may still be running."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to release agent: {str(e)}")


@router.get("/{agent_name}/info")
async def get_agent_info_endpoint(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get template info and metadata for an agent.

    Returns the agent's template.yaml metadata including:
    - display_name, description, version, author
    - capabilities, sub_agents, commands, platforms
    - resource allocation
    """
    import httpx

    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    container.reload()

    # If agent is not running, return basic info from container labels
    if container.status != "running":
        labels = container.labels
        return {
            "has_template": bool(labels.get("trinity.template")),
            "agent_name": agent_name,
            "template_name": labels.get("trinity.template", ""),
            "type": labels.get("trinity.agent-type", ""),
            "resources": {
                "cpu": labels.get("trinity.cpu", ""),
                "memory": labels.get("trinity.memory", "")
            },
            "status": "stopped",
            "message": "Agent is stopped. Start the agent to see full template info."
        }

    # Agent is running - fetch template info from agent's internal API
    try:
        agent_url = f"http://agent-{agent_name}:8000/api/template/info"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(agent_url)
            if response.status_code == 200:
                data = response.json()
                data["status"] = "running"
                return data
            else:
                # Fallback to container labels if endpoint not available
                labels = container.labels
                return {
                    "has_template": bool(labels.get("trinity.template")),
                    "agent_name": agent_name,
                    "template_name": labels.get("trinity.template", ""),
                    "type": labels.get("trinity.agent-type", ""),
                    "resources": {
                        "cpu": labels.get("trinity.cpu", ""),
                        "memory": labels.get("trinity.memory", "")
                    },
                    "status": "running",
                    "message": "Template info endpoint not available in this agent version"
                }
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Agent is starting up, please try again")
    except Exception as e:
        labels = container.labels
        return {
            "has_template": bool(labels.get("trinity.template")),
            "agent_name": agent_name,
            "template_name": labels.get("trinity.template", ""),
            "type": labels.get("trinity.agent-type", ""),
            "resources": {
                "cpu": labels.get("trinity.cpu", ""),
                "memory": labels.get("trinity.memory", "")
            },
            "status": "running",
            "message": f"Could not fetch template info: {str(e)}"
        }


@router.get("/{agent_name}/files")
async def list_agent_files_endpoint(
    agent_name: str,
    request: Request,
    path: str = "/home/developer",
    current_user: User = Depends(get_current_user)
):
    """
    List files in the agent's workspace directory.
    Returns a flat list of files with metadata (name, size, modified date).
    """
    import httpx

    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    container.reload()
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to browse files")

    try:
        # Call agent's internal file listing API
        agent_url = f"http://agent-{agent_name}:8000/api/files"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(agent_url, params={"path": path})
            if response.status_code == 200:
                # Audit log the file list access
                await log_audit_event(
                    event_type="file_access",
                    action="file_list",
                    user_id=current_user.username,
                    agent_name=agent_name,
                    ip_address=request.client.host if request.client else None,
                    details={"path": path},
                    result="success"
                )
                return response.json()
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to list files: {response.text}"
                )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="File listing timed out")
    except HTTPException:
        raise
    except Exception as e:
        await log_audit_event(
            event_type="file_access",
            action="file_list",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            details={"path": path, "error": str(e)},
            result="error"
        )
        raise HTTPException(status_code=500, detail=f"Failed to list files: {str(e)}")


@router.get("/{agent_name}/files/download")
async def download_agent_file_endpoint(
    agent_name: str,
    request: Request,
    path: str,
    current_user: User = Depends(get_current_user)
):
    """
    Download a file from the agent's workspace.
    Returns the file content as plain text.
    """
    import httpx
    from fastapi.responses import PlainTextResponse

    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    container.reload()
    if container.status != "running":
        raise HTTPException(status_code=400, detail="Agent must be running to download files")

    try:
        # Call agent's internal file download API
        agent_url = f"http://agent-{agent_name}:8000/api/files/download"
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(agent_url, params={"path": path})
            if response.status_code == 200:
                # Audit log the file download
                await log_audit_event(
                    event_type="file_access",
                    action="file_download",
                    user_id=current_user.username,
                    agent_name=agent_name,
                    ip_address=request.client.host if request.client else None,
                    details={"file_path": path},
                    result="success"
                )
                return PlainTextResponse(content=response.text)
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to download file: {response.text}"
                )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="File download timed out")
    except HTTPException:
        raise
    except Exception as e:
        await log_audit_event(
            event_type="file_access",
            action="file_download",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            details={"file_path": path, "error": str(e)},
            result="error"
        )
        raise HTTPException(status_code=500, detail=f"Failed to download file: {str(e)}")


# ============================================================================
# Activity Stream Endpoints (Phase 9.7)
# ============================================================================

@router.get("/{agent_name}/activities")
async def get_agent_activities(
    agent_name: str,
    activity_type: Optional[str] = None,
    activity_state: Optional[str] = None,
    limit: int = 100,
    current_user: User = Depends(get_current_user)
):
    """
    Get activity history for a specific agent.

    Filters:
    - activity_type: chat_start, chat_end, tool_call, schedule_start, schedule_end, agent_collaboration
    - activity_state: started, completed, failed
    - limit: max number of activities to return (default 100)
    """
    # Check user permissions
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    # Verify agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get activities from database
    activities = db.get_agent_activities(
        agent_name=agent_name,
        activity_type=activity_type,
        activity_state=activity_state,
        limit=limit
    )

    return {
        "agent_name": agent_name,
        "count": len(activities),
        "activities": activities
    }


@router.get("/activities/timeline")
async def get_activity_timeline(
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    activity_types: Optional[str] = None,  # Comma-separated list
    limit: int = 100,
    current_user: User = Depends(get_current_user)
):
    """
    Get cross-agent activity timeline.

    Filters:
    - start_time: ISO8601 timestamp (e.g., "2025-12-02T10:00:00")
    - end_time: ISO8601 timestamp
    - activity_types: comma-separated list (e.g., "chat_start,tool_call")
    - limit: max number of activities to return (default 100)

    Note: Only returns activities for agents the user has access to.
    """
    # Parse activity types if provided
    types_list = activity_types.split(",") if activity_types else None

    # Get all activities
    all_activities = db.get_activities_in_range(
        start_time=start_time,
        end_time=end_time,
        activity_types=types_list,
        limit=limit * 2  # Get more since we'll filter by access
    )

    # Filter activities to only include agents the user can access
    filtered_activities = []
    for activity in all_activities:
        agent_name = activity.get("agent_name")
        if db.can_user_access_agent(current_user.username, agent_name):
            filtered_activities.append(activity)
            if len(filtered_activities) >= limit:
                break

    return {
        "count": len(filtered_activities),
        "start_time": start_time,
        "end_time": end_time,
        "activity_types": types_list,
        "timeline": filtered_activities
    }


# ============================================================================
# Agent Permissions Endpoints (Phase 9.10: Agent-to-Agent Permissions)
# ============================================================================

@router.get("/{agent_name}/permissions")
async def get_agent_permissions(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get permissions for an agent.

    Returns:
    - source_agent: The agent name
    - permitted_agents: List of agents this agent can communicate with
    - available_agents: List of all other accessible agents with permission status
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get permitted agents
    permitted_list = db.get_permitted_agents(agent_name)

    # Get all agents accessible to this user
    accessible_agents = get_accessible_agents(current_user)

    # Build available agents list with permission status
    available_agents = []
    permitted_agents = []

    for agent in accessible_agents:
        if agent["name"] == agent_name:
            continue  # Skip self

        agent_info = {
            "name": agent["name"],
            "status": agent["status"],
            "type": agent.get("type", ""),
            "permitted": agent["name"] in permitted_list
        }

        if agent_info["permitted"]:
            permitted_agents.append(agent_info)
        available_agents.append(agent_info)

    return {
        "source_agent": agent_name,
        "permitted_agents": permitted_agents,
        "available_agents": available_agents
    }


@router.put("/{agent_name}/permissions")
async def set_agent_permissions(
    agent_name: str,
    request: Request,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    """
    Set permissions for an agent (full replacement).

    Body:
    - permitted_agents: List of agent names to permit
    """
    # Only owner or admin can modify permissions
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can modify agent permissions")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    permitted_agents = body.get("permitted_agents", [])

    # Validate all target agents exist and are accessible
    for target in permitted_agents:
        if not db.can_user_access_agent(current_user.username, target):
            raise HTTPException(
                status_code=400,
                detail=f"Agent '{target}' does not exist or is not accessible"
            )

    # Set permissions
    db.set_agent_permissions(agent_name, permitted_agents, current_user.username)

    await log_audit_event(
        event_type="agent_permissions",
        action="set_permissions",
        user_id=current_user.username,
        agent_name=agent_name,
        ip_address=request.client.host if request.client else None,
        result="success",
        details={"permitted_count": len(permitted_agents)}
    )

    return {
        "status": "updated",
        "source_agent": agent_name,
        "permitted_count": len(permitted_agents)
    }


@router.post("/{agent_name}/permissions/{target_agent}")
async def add_agent_permission(
    agent_name: str,
    target_agent: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Add permission for an agent to communicate with another agent.
    """
    # Only owner or admin can modify permissions
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can modify agent permissions")

    # Verify source agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify target agent exists and is accessible
    if not db.can_user_access_agent(current_user.username, target_agent):
        raise HTTPException(status_code=400, detail=f"Target agent '{target_agent}' does not exist or is not accessible")

    # Can't permit self
    if agent_name == target_agent:
        raise HTTPException(status_code=400, detail="Agent cannot be permitted to call itself")

    # Add permission
    result = db.add_agent_permission(agent_name, target_agent, current_user.username)

    if result:
        await log_audit_event(
            event_type="agent_permissions",
            action="add_permission",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="success",
            details={"target_agent": target_agent}
        )
        return {"status": "added", "source_agent": agent_name, "target_agent": target_agent}
    else:
        return {"status": "already_exists", "source_agent": agent_name, "target_agent": target_agent}


@router.delete("/{agent_name}/permissions/{target_agent}")
async def remove_agent_permission(
    agent_name: str,
    target_agent: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Remove permission for an agent to communicate with another agent.
    """
    # Only owner or admin can modify permissions
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can modify agent permissions")

    # Verify source agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Remove permission
    removed = db.remove_agent_permission(agent_name, target_agent)

    if removed:
        await log_audit_event(
            event_type="agent_permissions",
            action="remove_permission",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="success",
            details={"target_agent": target_agent}
        )
        return {"status": "removed", "source_agent": agent_name, "target_agent": target_agent}
    else:
        return {"status": "not_found", "source_agent": agent_name, "target_agent": target_agent}


# ============================================================================
# Custom Metrics Endpoints (Phase 9.9)
# ============================================================================

@router.get("/{agent_name}/metrics")
async def get_agent_metrics(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get agent custom metrics.

    Returns metric definitions from agent's template.yaml and current values
    from metrics.json in the agent's workspace.

    Metric types supported:
    - counter: Monotonically increasing value
    - gauge: Current value that can go up/down
    - percentage: 0-100 value with progress bar
    - status: Enum/state value with colored badge
    - duration: Time in seconds (formatted)
    - bytes: Size in bytes (formatted)

    Returns:
    - agent_name: Name of the agent
    - has_metrics: Whether agent has custom metrics defined
    - definitions: List of metric definitions from template.yaml
    - values: Current metric values from metrics.json
    - last_updated: Timestamp from metrics.json (if available)
    - status: Agent status (running/stopped)
    """
    import httpx

    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    container.reload()

    # If agent is not running, return basic info
    if container.status != "running":
        return {
            "agent_name": agent_name,
            "has_metrics": False,
            "status": "stopped",
            "message": "Agent must be running to read metrics"
        }

    # Agent is running - fetch metrics from agent's internal API
    try:
        agent_url = f"http://agent-{agent_name}:8000/api/metrics"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(agent_url)
            if response.status_code == 200:
                data = response.json()
                data["agent_name"] = agent_name
                data["status"] = "running"
                return data
            else:
                return {
                    "agent_name": agent_name,
                    "has_metrics": False,
                    "status": "running",
                    "message": f"Failed to fetch metrics: HTTP {response.status_code}"
                }
    except httpx.TimeoutException:
        return {
            "agent_name": agent_name,
            "has_metrics": False,
            "status": "running",
            "message": "Agent is starting up, please try again"
        }
    except Exception as e:
        return {
            "agent_name": agent_name,
            "has_metrics": False,
            "status": "running",
            "message": f"Failed to read metrics: {str(e)}"
        }


# ============================================================================
# Shared Folders Endpoints (Phase 9.11: Agent Shared Folders)
# ============================================================================

@router.get("/{agent_name}/folders")
async def get_agent_folders(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get shared folder configuration for an agent.

    Returns:
    - expose_enabled: Whether this agent exposes a shared folder
    - consume_enabled: Whether this agent mounts other agents' shared folders
    - exposed_volume: Volume name if exposing
    - consumed_folders: List of mounted folders from other agents
    - restart_required: Whether config changed and restart is needed
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get config from database (or defaults if none)
    config = db.get_shared_folder_config(agent_name)
    expose_enabled = config.expose_enabled if config else False
    consume_enabled = config.consume_enabled if config else False

    # Get actual mounted volumes from container
    container.reload()
    mounts = container.attrs.get("Mounts", [])

    # Check if agent is exposing (has /home/developer/shared-out mount)
    shared_out_mounted = any(
        m.get("Destination") == "/home/developer/shared-out"
        for m in mounts
    )

    # Build list of consumed folders
    consumed_folders = []
    if consume_enabled:
        available = db.get_available_shared_folders(agent_name)
        for source_agent in available:
            mount_path = db.get_shared_mount_path(source_agent)
            source_volume = db.get_shared_volume_name(source_agent)

            # Check if this volume is actually mounted
            is_mounted = any(
                m.get("Destination") == mount_path
                for m in mounts
            )

            consumed_folders.append({
                "source_agent": source_agent,
                "mount_path": mount_path,
                "access_mode": "rw",
                "currently_mounted": is_mounted
            })

    # Determine if restart is required
    # Restart needed if config says expose but not mounted (or vice versa)
    restart_required = (expose_enabled != shared_out_mounted)

    # Also check consume status
    if consume_enabled and consumed_folders:
        # Check if any expected mounts are missing
        for folder in consumed_folders:
            if not folder["currently_mounted"]:
                restart_required = True
                break

    return {
        "agent_name": agent_name,
        "expose_enabled": expose_enabled,
        "consume_enabled": consume_enabled,
        "exposed_volume": db.get_shared_volume_name(agent_name) if expose_enabled else None,
        "exposed_path": "/home/developer/shared-out",
        "consumed_folders": consumed_folders,
        "restart_required": restart_required,
        "status": container.status
    }


@router.put("/{agent_name}/folders")
async def update_agent_folders(
    agent_name: str,
    request: Request,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    """
    Update shared folder configuration for an agent.

    Body:
    - expose_enabled: (optional) Whether to expose a shared folder
    - consume_enabled: (optional) Whether to mount other agents' shared folders

    Note: Changes require agent restart to take effect.
    """
    # Only owner can modify folder sharing
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can modify folder sharing")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    expose_enabled = body.get("expose_enabled")
    consume_enabled = body.get("consume_enabled")

    # Update config
    config = db.upsert_shared_folder_config(
        agent_name,
        expose_enabled=expose_enabled,
        consume_enabled=consume_enabled
    )

    await log_audit_event(
        event_type="agent_folders",
        action="update_config",
        user_id=current_user.username,
        agent_name=agent_name,
        ip_address=request.client.host if request.client else None,
        result="success",
        details={
            "expose_enabled": config.expose_enabled,
            "consume_enabled": config.consume_enabled
        }
    )

    return {
        "status": "updated",
        "agent_name": agent_name,
        "expose_enabled": config.expose_enabled,
        "consume_enabled": config.consume_enabled,
        "restart_required": True,
        "message": "Configuration updated. Restart the agent to apply changes."
    }


@router.get("/{agent_name}/folders/available")
async def get_available_shared_folders(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get list of shared folders available for this agent to mount.

    Returns agents that:
    1. Have expose_enabled=True
    2. This agent has permission to communicate with

    Useful for showing which folders can be mounted.
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    available = db.get_available_shared_folders(agent_name)

    # Build detailed list with mount info
    available_folders = []
    for source_agent in available:
        source_container = get_agent_container(source_agent)
        available_folders.append({
            "source_agent": source_agent,
            "volume_name": db.get_shared_volume_name(source_agent),
            "mount_path": db.get_shared_mount_path(source_agent),
            "source_status": source_container.status if source_container else "not_found"
        })

    return {
        "agent_name": agent_name,
        "available_folders": available_folders,
        "count": len(available_folders)
    }


@router.get("/{agent_name}/folders/consumers")
async def get_folder_consumers(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get list of agents that can consume this agent's shared folder.

    Returns agents that:
    1. Have consume_enabled=True
    2. Have permission to communicate with this agent

    Useful for understanding who will see exposed files.
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get agents that can consume from this agent
    consumers = db.get_consuming_agents(agent_name)

    consumer_list = []
    for consumer_agent in consumers:
        consumer_container = get_agent_container(consumer_agent)
        consumer_list.append({
            "agent_name": consumer_agent,
            "mount_path": db.get_shared_mount_path(agent_name),
            "status": consumer_container.status if consumer_container else "not_found"
        })

    return {
        "source_agent": agent_name,
        "consumers": consumer_list,
        "count": len(consumer_list)
    }


# ============================================================================
# API KEY SETTINGS
# ============================================================================

@router.get("/{agent_name}/api-key-setting")
async def get_agent_api_key_setting(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get the API key setting for an agent.

    Returns whether the agent uses the platform API key or relies on
    terminal-based authentication.
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    use_platform_key = db.get_use_platform_api_key(agent_name)

    # Check if current container matches the setting
    container.reload()
    env_matches = _check_api_key_env_matches(container, agent_name)

    return {
        "agent_name": agent_name,
        "use_platform_api_key": use_platform_key,
        "restart_required": not env_matches,
        "status": container.status
    }


@router.put("/{agent_name}/api-key-setting")
async def update_agent_api_key_setting(
    agent_name: str,
    request: Request,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    """
    Update the API key setting for an agent.

    Body:
    - use_platform_api_key: True to use Trinity's platform key, False to require terminal auth

    Note: Changes require agent restart to take effect.
    """
    # Only owner can modify this setting
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can modify API key settings")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    use_platform_key = body.get("use_platform_api_key")
    if use_platform_key is None:
        raise HTTPException(status_code=400, detail="use_platform_api_key is required")

    # Update setting
    db.set_use_platform_api_key(agent_name, bool(use_platform_key))

    await log_audit_event(
        event_type="agent_settings",
        action="update_api_key_setting",
        user_id=current_user.username,
        agent_name=agent_name,
        ip_address=request.client.host if request.client else None,
        result="success",
        details={
            "use_platform_api_key": use_platform_key
        }
    )

    return {
        "status": "updated",
        "agent_name": agent_name,
        "use_platform_api_key": use_platform_key,
        "restart_required": True,
        "message": "Setting updated. Restart the agent to apply changes."
    }


# ============================================================================
# TERMINAL WEBSOCKET ENDPOINT
# ============================================================================

@router.websocket("/{agent_name}/terminal")
async def agent_terminal(
    websocket: WebSocket,
    agent_name: str,
    mode: str = Query(default="claude")  # 'claude' or 'bash'
):
    """
    Interactive terminal WebSocket for any agent.

    Provides full PTY-based terminal access to an agent container.
    Supports both Claude Code and bash shell modes.

    Authentication:
    - First message must be JSON: {"type": "auth", "token": "<jwt_token>"}
    - User must have access to the agent (owner, shared, or admin)

    Control Messages (JSON):
    - {"type": "resize", "cols": 80, "rows": 24} - Resize terminal

    All other messages are forwarded as terminal input.
    """
    await websocket.accept()

    user_email = None
    exec_id = None
    docker_socket = None
    session_start = None
    session_key = None

    try:
        # Step 1: Authenticate via first message
        try:
            auth_msg = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            auth_data = json.loads(auth_msg)

            if auth_data.get("type") != "auth":
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Expected auth message first",
                    "close": True
                }))
                await websocket.close(code=4001, reason="Expected auth message")
                return

            token = auth_data.get("token")
            if not token:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Token required",
                    "close": True
                }))
                await websocket.close(code=4001, reason="Token required")
                return

            # Decode and validate token
            user = decode_token(token)
            if not user:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Invalid token",
                    "close": True
                }))
                await websocket.close(code=4001, reason="Invalid token")
                return

            user_email = user.get("email") or user.get("sub") or "unknown"
            user_role = user.get("role", "user")

            # Check access to this agent
            if not db.can_user_access_agent(user_email, agent_name) and user_role != "admin":
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "You don't have access to this agent",
                    "close": True
                }))
                await websocket.close(code=4003, reason="Access denied")
                return

        except asyncio.TimeoutError:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "Authentication timeout",
                "close": True
            }))
            await websocket.close(code=4001, reason="Auth timeout")
            return
        except json.JSONDecodeError:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "Invalid JSON in auth message",
                "close": True
            }))
            await websocket.close(code=4001, reason="Invalid auth format")
            return

        # Step 2: Check for existing session (limit 1 per user per agent)
        SESSION_TIMEOUT_SECONDS = 300  # 5 minutes stale session cleanup
        session_key = (user_email, agent_name)
        with _terminal_lock:
            if session_key in _active_terminal_sessions:
                session_info = _active_terminal_sessions[session_key]
                session_age = (datetime.utcnow() - session_info["started_at"]).total_seconds()
                if session_age < SESSION_TIMEOUT_SECONDS:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "You already have an active terminal session for this agent. Close it first.",
                        "close": True
                    }))
                    await websocket.close(code=4002, reason="Session limit reached")
                    return
                else:
                    # Stale session, clean it up
                    logger.warning(f"Cleaning up stale terminal session for {user_email}@{agent_name} (age: {session_age:.0f}s)")
            _active_terminal_sessions[session_key] = {"started_at": datetime.utcnow()}

        # Step 3: Get container
        container = get_agent_container(agent_name)
        if not container:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": f"Agent '{agent_name}' container not found",
                "close": True
            }))
            with _terminal_lock:
                _active_terminal_sessions.pop(session_key, None)
            await websocket.close(code=4004, reason="Container not found")
            return

        container.reload()
        if container.status != "running":
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": f"Agent '{agent_name}' is not running. Please start it first.",
                "close": True
            }))
            with _terminal_lock:
                _active_terminal_sessions.pop(session_key, None)
            await websocket.close(code=4004, reason="Container not running")
            return

        # Step 4: Audit log - session start
        session_start = datetime.utcnow()
        await log_audit_event(
            event_type="terminal_session",
            action="start",
            user_id=user_email,
            agent_name=agent_name,
            details={"mode": mode}
        )

        # Step 5: Create exec with TTY
        cmd = ["claude"] if mode == "claude" else ["/bin/bash"]

        # Use docker API to create exec instance
        exec_instance = docker_client.api.exec_create(
            container.id,
            cmd,
            stdin=True,
            tty=True,
            stdout=True,
            stderr=True,
            user="developer",
            workdir="/home/developer",
            environment={"TERM": "xterm-256color", "COLORTERM": "truecolor"}
        )
        exec_id = exec_instance["Id"]

        # Start exec and get socket
        exec_output = docker_client.api.exec_start(exec_id, socket=True, tty=True)

        # Get the raw socket
        docker_socket = exec_output._sock
        docker_socket.setblocking(False)

        # Send success message
        await websocket.send_text(json.dumps({"type": "auth_success"}))

        logger.info(f"Terminal session started for {user_email}@{agent_name} (mode: {mode})")

        # Step 6: Bidirectional forwarding
        loop = asyncio.get_event_loop()

        async def read_from_docker():
            """Read from Docker socket, send to WebSocket."""
            while True:
                try:
                    # Use select for non-blocking read with timeout
                    ready = await loop.run_in_executor(
                        None,
                        lambda: select.select([docker_socket], [], [], 0.1)
                    )
                    if ready[0]:
                        data = await loop.run_in_executor(
                            None,
                            lambda: docker_socket.recv(4096)
                        )
                        if not data:
                            break
                        await websocket.send_bytes(data)
                    else:
                        # No data available, yield to other tasks
                        await asyncio.sleep(0.01)
                except Exception as e:
                    logger.debug(f"Docker read error for {agent_name}: {e}")
                    break

        async def read_from_websocket():
            """Read from WebSocket, send to Docker socket."""
            while True:
                try:
                    message = await websocket.receive()

                    if message["type"] == "websocket.disconnect":
                        break

                    if "text" in message:
                        # Check if it's a control message
                        try:
                            ctrl = json.loads(message["text"])
                            if ctrl.get("type") == "resize":
                                # Resize the PTY
                                cols = ctrl.get("cols", 80)
                                rows = ctrl.get("rows", 24)
                                docker_client.api.exec_resize(
                                    exec_id,
                                    height=rows,
                                    width=cols
                                )
                                continue
                        except json.JSONDecodeError:
                            pass

                        # Plain text input - send to container
                        await loop.run_in_executor(
                            None,
                            lambda: docker_socket.sendall(message["text"].encode())
                        )

                    elif "bytes" in message:
                        await loop.run_in_executor(
                            None,
                            lambda: docker_socket.sendall(message["bytes"])
                        )

                except WebSocketDisconnect:
                    break
                except Exception as e:
                    logger.debug(f"WebSocket read error for {agent_name}: {e}")
                    break

        # Run both tasks concurrently
        await asyncio.gather(
            read_from_docker(),
            read_from_websocket(),
            return_exceptions=True
        )

    except WebSocketDisconnect:
        logger.info(f"Terminal WebSocket disconnected for {user_email}@{agent_name}")
    except Exception as e:
        logger.error(f"Terminal session error for {agent_name}: {e}")
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": str(e)
            }))
        except:
            pass
    finally:
        # Cleanup
        if docker_socket:
            try:
                docker_socket.close()
            except:
                pass

        # Remove from active sessions
        if session_key:
            with _terminal_lock:
                _active_terminal_sessions.pop(session_key, None)

            # Audit log - session end
            session_duration = None
            if session_start:
                session_duration = (datetime.utcnow() - session_start).total_seconds()

            try:
                await log_audit_event(
                    event_type="terminal_session",
                    action="end",
                    user_id=user_email,
                    agent_name=agent_name,
                    details={
                        "mode": mode,
                        "duration_seconds": session_duration
                    }
                )
            except:
                pass

            if session_duration:
                logger.info(f"Terminal session ended for {user_email}@{agent_name} (duration: {session_duration:.1f}s)")
            else:
                logger.info(f"Terminal session ended for {user_email}@{agent_name}")

