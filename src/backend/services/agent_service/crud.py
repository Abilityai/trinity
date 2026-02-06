"""
Agent Service CRUD - Agent creation and deletion operations.

Contains the core logic for creating and deleting agents.
"""
import os
import json
import docker
import logging
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import HTTPException, Request

from models import AgentConfig, AgentStatus, User
from database import db
from services.docker_service import (
    docker_client,
    get_agent_by_name,
    get_next_available_port,
    get_agent_status_from_container,
)
from services.template_service import (
    get_github_template,
    generate_credential_files,
    find_template_file,
)
from services import git_service
from services.settings_service import get_anthropic_api_key, get_github_pat, get_agent_full_capabilities
from utils.helpers import sanitize_agent_name
from .lifecycle import RESTRICTED_CAPABILITIES, FULL_CAPABILITIES

logger = logging.getLogger(__name__)


def get_platform_version() -> str:
    """Get the current Trinity platform version from VERSION file."""
    version_paths = [
        Path("/app/VERSION"),  # In container
        Path(__file__).parent.parent.parent.parent.parent / "VERSION",  # Development
    ]
    for version_path in version_paths:
        if version_path.exists():
            return version_path.read_text().strip()
    return "unknown"


async def create_agent_internal(
    config: AgentConfig,
    current_user: User,
    request: Request,
    skip_name_sanitization: bool = False,
    credential_manager=None,
    ws_manager=None
) -> AgentStatus:
    """
    Internal function to create an agent.

    Used by both the API endpoint and system deployment.

    Args:
        config: Agent configuration
        current_user: Authenticated user
        request: FastAPI request object
        skip_name_sanitization: If True, don't sanitize the name (used when name is pre-validated)
        credential_manager: The credential manager instance
        ws_manager: Optional WebSocket manager for broadcasts

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
    git_instance_id = None
    git_working_branch = None
    # Phase 9.11: Track shared folder config from template
    template_shared_folders = None

    # Load template configuration
    if config.template:
        if config.template.startswith("github:"):
            gh_template = get_github_template(config.template)

            if gh_template:
                # Pre-defined GitHub template from config.py
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
            else:
                # Dynamic GitHub template - use any github:owner/repo format
                # Requires system GitHub PAT to be configured
                repo_path = config.template[7:]  # Remove "github:" prefix
                if "/" not in repo_path:
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid GitHub template format. Use: github:owner/repo"
                    )

                # Get system GitHub PAT from settings (SQLite) or env var
                github_pat = get_github_pat()
                if not github_pat:
                    raise HTTPException(
                        status_code=500,
                        detail="GitHub PAT not configured. Set GITHUB_PAT in .env or add via Settings."
                    )

                github_repo_for_agent = repo_path
                github_pat_for_agent = github_pat
                logger.info(f"Using dynamic GitHub template: {repo_path}")

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
            template_yaml = find_template_file(template_path)

            if template_yaml:
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
                        # Multi-runtime support - extract runtime config from template
                        runtime_config = template_data.get("runtime", {})
                        if isinstance(runtime_config, dict):
                            config.runtime = runtime_config.get("type", config.runtime)
                            config.runtime_model = runtime_config.get("model", config.runtime_model)
                        elif isinstance(runtime_config, str):
                            config.runtime = runtime_config
                        # Phase 9.11: Extract shared folder config from template
                        shared_folders_config = template_data.get("shared_folders", {})
                        if shared_folders_config:
                            template_shared_folders = {
                                "expose": shared_folders_config.get("expose", False),
                                "consume": shared_folders_config.get("consume", False)
                            }
                except Exception as e:
                    logger.warning(f"Error loading template config: {e}")

    if config.port is None:
        config.port = get_next_available_port()

    # Get only explicitly assigned credentials (no longer auto-inject all user credentials)
    flat_credentials = credential_manager.get_assigned_credential_values(config.name, current_user.username)

    # For backward compatibility with MCP server assignments, also check those
    agent_credentials = credential_manager.get_agent_credentials(config.name, config.mcp_servers, current_user.username)
    for server, creds in agent_credentials.items():
        if isinstance(creds, dict):
            for key, value in creds.items():
                if key.upper() not in flat_credentials:
                    flat_credentials[key.upper()] = value
        elif server.upper() not in flat_credentials:
            flat_credentials[server.upper()] = creds

    generated_files = {}
    if template_data:
        generated_files = generate_credential_files(
            template_data, flat_credentials, config.name,
            template_base_path=github_template_path
        )

    # Get only explicitly assigned file-type credentials (e.g., service account JSON files)
    file_credentials = credential_manager.get_assigned_file_credentials(config.name, current_user.username)
    if file_credentials:
        logger.info(f"Injecting {len(file_credentials)} assigned credential file(s) for agent {config.name}")

    cred_files_dir = Path(f"/tmp/agent-{config.name}-creds")
    cred_files_dir.mkdir(exist_ok=True)

    # Write template-generated files (.env, .mcp.json, etc.)
    for filepath, content in generated_files.items():
        file_path = cred_files_dir / filepath
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w") as f:
            f.write(content)

    # Write file-type credentials to their target paths
    # These will be mounted at /generated-creds/ and copied by startup.sh
    for filepath, content in file_credentials.items():
        # Store in a special "credential-files" subdirectory to distinguish from other generated files
        file_path = cred_files_dir / "credential-files" / filepath
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w") as f:
            f.write(content)
        logger.debug(f"Wrote credential file: {filepath}")

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
            logger.info(f"Created MCP API key for agent {config.name}: {agent_mcp_key.key_prefix}...")
    except Exception as e:
        logger.warning(f"Failed to create MCP API key for agent {config.name}: {e}")

    env_vars = {
        'AGENT_NAME': config.name,
        'AGENT_TYPE': config.type,
        'CREDENTIALS_FILE': '/config/credentials.json',
        'ANTHROPIC_API_KEY': get_anthropic_api_key(),
        'ENABLE_SSH': 'true',
        'ENABLE_AGENT_UI': 'true',
        'AGENT_SERVER_PORT': '8000',
        'TEMPLATE_NAME': config.template if config.template else '',
        # Multi-runtime support
        'AGENT_RUNTIME': config.runtime or 'claude-code',
        'AGENT_RUNTIME_MODEL': config.runtime_model or ''
    }

    # Add Google API key if using Gemini runtime
    # Gemini CLI expects GEMINI_API_KEY environment variable
    if config.runtime == 'gemini-cli' or config.runtime == 'gemini':
        google_api_key = os.getenv('GOOGLE_API_KEY', '')
        if google_api_key:
            env_vars['GEMINI_API_KEY'] = google_api_key  # Gemini CLI expects this name
        else:
            logger.warning("Gemini runtime selected but GOOGLE_API_KEY not configured")

    # OpenTelemetry Configuration (enabled by default)
    # Claude Code has built-in OTel support - these vars enable metrics export
    if os.getenv('OTEL_ENABLED', '1') == '1':
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

        # Source mode (default): Track source branch directly for pull-only sync
        # Legacy mode: Create a unique working branch for bidirectional sync
        if config.source_mode:
            env_vars['GIT_SOURCE_MODE'] = 'true'
            env_vars['GIT_SOURCE_BRANCH'] = config.source_branch or 'main'
        else:
            env_vars['GIT_WORKING_BRANCH'] = git_working_branch

    for server, creds in agent_credentials.items():
        server_upper = server.upper().replace('-', '_')
        # Handle both dict credentials (from explicit assignment) and
        # string credentials (from bulk import where key=ENV_VAR, value=string)
        if isinstance(creds, str):
            # Bulk-imported credential: server is the env var name, creds is the value
            env_vars[server_upper] = creds
        elif isinstance(creds, dict):
            # Explicitly assigned credential with structured data
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
            # First, write template-defined shared folder config to DB (if defined)
            if template_shared_folders:
                try:
                    db.upsert_shared_folder_config(
                        agent_name=config.name,
                        expose_enabled=template_shared_folders.get("expose", False),
                        consume_enabled=template_shared_folders.get("consume", False)
                    )
                    logger.info(f"Applied template shared folder config for {config.name}: expose={template_shared_folders.get('expose')}, consume={template_shared_folders.get('consume')}")
                except Exception as e:
                    logger.warning(f"Failed to apply template shared folder config for {config.name}: {e}")

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

            # Get system-wide full_capabilities setting (not per-agent)
            full_capabilities = get_agent_full_capabilities()

            # Create container with security settings
            # Security principle: ALWAYS apply baseline security, even in full_capabilities mode
            # - Always drop ALL caps, then add back only what's needed
            # - Always apply AppArmor profile
            # - Always apply noexec,nosuid to /tmp
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
                    'trinity.template': config.template or '',
                    'trinity.agent-runtime': config.runtime or 'claude-code',
                    'trinity.full-capabilities': str(full_capabilities).lower(),
                    'trinity.base-image-version': get_platform_version()
                },
                # Always apply AppArmor for additional sandboxing
                security_opt=['apparmor:docker-default'],
                # Always drop ALL capabilities first (defense in depth)
                cap_drop=['ALL'],
                # Add back only the capabilities needed for the mode
                cap_add=FULL_CAPABILITIES if full_capabilities else RESTRICTED_CAPABILITIES,
                read_only=False,
                # Always apply noexec,nosuid to /tmp for security
                tmpfs={'/tmp': 'noexec,nosuid,size=100m'},
                network='trinity-agent-network',
                mem_limit=config.resources.get('memory', '4Gi'),
                cpu_count=int(config.resources.get('cpu', '2'))
            )

            agent_status = get_agent_status_from_container(container)

            if ws_manager:
                await ws_manager.broadcast(json.dumps({
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
                    logger.info(f"Granted {permissions_count} default permissions for agent {config.name}")
            except Exception as e:
                logger.warning(f"Failed to grant default permissions for {config.name}: {e}")

            # Phase 7: Create git config for GitHub-native agents
            if github_repo_for_agent:
                try:
                    # In source mode, working_branch = source_branch (no separate working branch)
                    effective_working_branch = config.source_branch if config.source_mode else git_working_branch
                    db.create_git_config(
                        agent_name=config.name,
                        github_repo=github_repo_for_agent,
                        working_branch=effective_working_branch,
                        instance_id=git_instance_id,
                        source_branch=config.source_branch or "main",
                        source_mode=config.source_mode
                    )
                except Exception as e:
                    logger.warning(f"Failed to create git config for {config.name}: {e}")

            return agent_status
        except Exception as e:
            logger.error(f"Failed to create agent {config.name}: {e}")
            raise HTTPException(status_code=500, detail="Failed to create agent. Please try again.")
    else:
        raise HTTPException(
            status_code=503,
            detail="Docker not available - cannot create agents in demo mode"
        )
