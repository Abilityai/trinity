"""
Agent Service CRUD - Agent creation and deletion operations.

Contains the core logic for creating and deleting agents.
"""
import os
import json
import docker
import logging
from datetime import datetime, timezone
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
from services.docker_utils import (
    volume_get, volume_create, containers_run
)
from services.template_service import (
    get_github_template,
    generate_credential_files,
)
from services import git_service
from services.settings_service import get_anthropic_api_key, get_github_pat, get_agent_full_capabilities, get_agent_quota_for_role
from services.github_service import GitHubService, GitHubError
from utils.helpers import sanitize_agent_name, utc_now_iso
from .helpers import validate_base_image
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
    ws_manager=None
) -> AgentStatus:
    """
    Internal function to create an agent.

    Used by both the API endpoint and system deployment.

    CRED-002: Credentials are no longer auto-injected during creation.
    They are added after creation via inject_credentials endpoint or
    imported from .credentials.enc on startup.

    Args:
        config: Agent configuration
        current_user: Authenticated user
        request: FastAPI request object
        skip_name_sanitization: If True, don't sanitize the name (used when name is pre-validated)
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

    if get_agent_by_name(config.name) or db.get_agent_owner(config.name):
        raise HTTPException(status_code=409, detail="Agent already exists")

    # Agent quota enforcement: per-role limits (QUOTA-001)
    max_agents = get_agent_quota_for_role(current_user.role)
    if max_agents > 0:
        owned = db.get_agents_by_owner(current_user.username)
        # System agents don't count toward user quota
        non_system = [a for a in owned if not (db.get_agent_owner(a) or {}).get("is_system")]
        if len(non_system) >= max_agents:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": f"Agent quota exceeded. You have {len(non_system)}/{max_agents} agents. "
                             f"Delete an agent to create a new one.",
                    "code": "QUOTA_EXCEEDED",
                    "current": len(non_system),
                    "limit": max_agents
                }
            )

    # SEC-172: Validate base image against allowlist before any Docker operations
    validate_base_image(config.base_image)

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
            # GIT-002: First, check if template URL contains @branch syntax
            # This applies to both pre-defined and dynamic templates
            template_str = config.template[7:]  # Remove "github:" prefix
            url_branch = None
            if "@" in template_str:
                template_str, url_branch = template_str.rsplit("@", 1)
                # Validate branch name (alphanumeric plus - _ /)
                if url_branch and url_branch.replace("-", "").replace("_", "").replace("/", "").isalnum():
                    config.source_branch = url_branch
                    logger.info(f"GIT-002: Parsed branch from URL: {url_branch}")
                else:
                    url_branch = None  # Invalid branch, ignore

            # Reconstruct template ID without branch for lookup
            template_lookup = f"github:{template_str}" if url_branch else config.template
            gh_template = get_github_template(template_lookup)

            if gh_template:
                # Pre-defined GitHub template from config.py
                github_repo = gh_template["github_repo"]

                # Get system GitHub PAT from settings (SQLite) or env var
                github_pat = get_github_pat()
                if not github_pat:
                    raise HTTPException(
                        status_code=500,
                        detail="GitHub PAT not configured. Set GITHUB_PAT in .env or add via Settings."
                    )

                github_repo_for_agent = github_repo
                github_pat_for_agent = github_pat
                config.resources = gh_template.get("resources", config.resources)
                config.mcp_servers = gh_template.get("mcp_servers", config.mcp_servers)
            else:
                # Dynamic GitHub template - use any github:owner/repo[@branch] format
                # Requires system GitHub PAT to be configured
                # Note: Branch was already parsed above; template_str already has branch removed
                repo_path = template_str

                if "/" not in repo_path:
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid GitHub template format. Use: github:owner/repo or github:owner/repo@branch"
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
                logger.info(f"Using dynamic GitHub template: {repo_path} (branch: {config.source_branch})")

            # Validate PAT has access to the repository before creating container
            # This prevents silent clone failures in startup.sh (#218)
            try:
                gh_service = GitHubService(github_pat_for_agent)
                repo_parts = github_repo_for_agent.split("/", 1)
                if len(repo_parts) == 2:
                    repo_info = await gh_service.check_repo_exists(repo_parts[0], repo_parts[1])
                    if not repo_info.exists:
                        raise HTTPException(
                            status_code=400,
                            detail=f"GitHub repository '{github_repo_for_agent}' not found or PAT does not have access. "
                                   f"Verify the repository exists and the configured GitHub PAT has read access."
                        )
                    logger.info(f"Validated GitHub repo access: {github_repo_for_agent} (private={repo_info.private})")

                    # If source_branch specified, validate branch exists
                    if config.source_branch and config.source_branch != repo_info.default_branch:
                        try:
                            branch_resp = await gh_service._request(
                                "GET", f"/repos/{github_repo_for_agent}/branches/{config.source_branch}"
                            )
                            if branch_resp.status_code == 404:
                                raise HTTPException(
                                    status_code=400,
                                    detail=f"Branch '{config.source_branch}' not found in repository '{github_repo_for_agent}'. "
                                           f"Available default branch: '{repo_info.default_branch}'."
                                )
                        except HTTPException:
                            raise
                        except Exception as e:
                            logger.warning(f"Could not validate branch '{config.source_branch}': {e}")
            except HTTPException:
                raise
            except GitHubError as e:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to validate GitHub repository access: {e}"
                )
            except Exception as e:
                # Log but don't block creation for transient network errors
                logger.warning(f"GitHub repo validation failed (non-blocking): {e}")

            # Generate git sync instance ID and branch for Phase 7.
            # S7 Layer 0 (#382): reserve the working branch atomically —
            # probes the remote with `git ls-remote` and inserts the DB
            # row under the partial UNIQUE index so no two agents can end
            # up bound to the same (repo, branch). The row is written
            # here, before the container is created, so it must be rolled
            # back if anything in the rest of the flow fails (see the
            # `try: ... except: db.delete_git_config(...)` block below).
            git_instance_id, git_working_branch = (
                await git_service.reserve_and_generate_instance_id(
                    agent_name=config.name,
                    github_repo=github_repo_for_agent,
                    source_branch=config.source_branch or "main",
                    source_mode=config.source_mode,
                )
            )
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

    # CRED-002: Credentials are now injected directly into agents after creation
    # via the inject_credentials endpoint, not auto-injected during creation.
    # The agent starts without credentials and they are added via Quick Inject
    # or imported from .credentials.enc files.

    generated_files = {}
    if template_data:
        # Generate empty credential files structure from template
        generated_files = generate_credential_files(
            template_data, {}, config.name,
            template_base_path=github_template_path
        )

    cred_files_dir = Path(f"/tmp/agent-{config.name}-creds")
    cred_files_dir.mkdir(exist_ok=True)

    # Write template-generated files (.env, .mcp.json, etc.)
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
            "credentials": {}  # CRED-002: Credentials injected after creation
        }
    }

    config_path = Path(f"/tmp/agent-{config.name}.yaml")
    with open(config_path, "w") as f:
        yaml.dump(agent_config, f)

    credentials_path = Path(f"/tmp/agent-{config.name}-credentials.json")
    with open(credentials_path, "w") as f:
        json.dump({}, f)  # CRED-002: Empty credentials, injected after creation

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

    # GUARD-001: per-agent guardrails overrides (empty by default; baseline
    # is always applied inside the container).
    _guardrails = db.get_guardrails_config(config.name)
    if _guardrails:
        import json as _json
        env_vars['AGENT_GUARDRAILS'] = _json.dumps(_guardrails)

    # Auto-assign subscription (round-robin) — #74
    auto_assigned_subscription_id = None
    try:
        least_used = db.get_least_used_subscription()
        if least_used:
            token = db.get_subscription_token(least_used.id)
            if token:
                env_vars['CLAUDE_CODE_OAUTH_TOKEN'] = token
                env_vars.pop('ANTHROPIC_API_KEY', None)
                auto_assigned_subscription_id = least_used.id
                logger.info(f"Auto-assigned subscription '{least_used.name}' to agent {config.name}")
            else:
                logger.warning(f"Failed to decrypt subscription '{least_used.name}' token, using platform API key")
    except Exception as e:
        logger.warning(f"Subscription auto-assign failed for {config.name}: {e}")

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
        # Dev/self-host: propagate optional git base-URL override to agent container
        _git_base = os.getenv('TRINITY_GIT_BASE_URL')
        if _git_base:
            env_vars['TRINITY_GIT_BASE_URL'] = _git_base

        # #389 S1a: 15-min auto-sync heartbeat. Only legacy (working-branch)
        # agents get it — source-mode agents track main read-only, and
        # auto-pushing to main would clobber protected branches. Operators
        # can toggle per-agent via PUT /api/agents/{name}/git/auto-sync.
        if not config.source_mode:
            env_vars['GIT_SYNC_AUTO'] = 'true'

        # Source mode (default): Track source branch directly for pull-only sync
        # Legacy mode: Create a unique working branch for bidirectional sync
        if config.source_mode:
            env_vars['GIT_SOURCE_MODE'] = 'true'
            env_vars['GIT_SOURCE_BRANCH'] = config.source_branch or 'main'
            logger.info(
                f"GitHub template env vars set for {config.name}: "
                f"repo={github_repo_for_agent}, branch={config.source_branch or 'main'}, "
                f"source_mode=true, sync=true"
            )
        else:
            env_vars['GIT_WORKING_BRANCH'] = git_working_branch
            logger.info(
                f"GitHub template env vars set for {config.name}: "
                f"repo={github_repo_for_agent}, working_branch={git_working_branch}, "
                f"source_mode=false, sync=true"
            )

    # CRED-002: Legacy credential injection loop removed.
    # Credentials are now injected after agent creation via:
    # - inject_credentials endpoint (Quick Inject)
    # - .credentials.enc import on agent startup

    if docker_client:
        try:
            # Create per-agent persistent volume for /home/developer (Pillar III: Persistent Memory)
            # This ensures files created by the agent survive container restarts
            agent_volume_name = f"agent-{config.name}-workspace"
            try:
                await volume_get(agent_volume_name)
            except docker.errors.NotFound:
                await volume_create(
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
                        await volume_get(shared_volume_name)
                    except docker.errors.NotFound:
                        await volume_create(
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
                            await containers_run(
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
                            await volume_get(source_volume)
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
            container = await containers_run(
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
                    'trinity.created': utc_now_iso(),
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

            # Persist auto-assigned subscription (#74)
            if auto_assigned_subscription_id:
                try:
                    db.assign_subscription_to_agent(config.name, auto_assigned_subscription_id)
                except Exception as e:
                    logger.warning(f"Failed to persist subscription assignment for {config.name}: {e}")

            # AVATAR-003: Seed avatar prompt from template
            _avatar_prompt = template_data.get("avatar_prompt") if template_data else None
            if _avatar_prompt:
                try:
                    db.set_default_avatar(config.name, _avatar_prompt, datetime.now(timezone.utc).isoformat())
                    logger.info(f"[AVATAR-003] Seeded avatar prompt from template for {config.name}")
                except Exception as e:
                    logger.warning(f"[AVATAR-003] Failed to seed avatar prompt for {config.name}: {e}")

            # Phase 9.10: Grant default permissions (Option B - same-owner agents)
            try:
                permissions_count = db.grant_default_permissions(config.name, current_user.username)
                if permissions_count > 0:
                    logger.info(f"Granted {permissions_count} default permissions for agent {config.name}")
            except Exception as e:
                logger.warning(f"Failed to grant default permissions for {config.name}: {e}")

            # Phase 7: git config was already reserved and persisted via
            # `reserve_and_generate_instance_id` earlier in this function
            # (S7 Layer 0). No second db.create_git_config call here — that
            # would either be a no-op (agent_name UNIQUE) or, worse, mask
            # a Layer 2 conflict.

            # S4 (#383): Materialize persistent-state allowlist into the agent.
            # Runtime sync/reset paths read `.trinity/persistent-state.yaml`;
            # template.yaml is only read at creation (10-min cache), so this
            # is the source of truth going forward. Non-fatal on failure —
            # reset operations fall back to the default list at read time.
            persistent_state = (
                (template_data or {}).get(
                    "persistent_state", git_service.DEFAULT_PERSISTENT_STATE
                )
            )
            try:
                await git_service.materialize_persistent_state(
                    config.name, persistent_state
                )
            except Exception as e:
                logger.warning(
                    f"[S4] Failed to materialize persistent-state.yaml for "
                    f"{config.name}: {e}"
                )

            # #389 S1a: opt non-source-mode GitHub-template agents into the
            # auto-sync heartbeat by default. Source-mode agents stay opt-in
            # (auto-pushing to main would clobber protected branches).
            if github_repo_for_agent and not config.source_mode:
                try:
                    db.set_git_auto_sync_enabled(config.name, True)
                except Exception as e:
                    logger.warning(
                        f"Failed to enable auto-sync for {config.name}: {e}"
                    )

            return agent_status
        except Exception as e:
            # S7 Layer 0 (#382): if anything after the reservation fails,
            # roll back the agent_git_config row so the working branch is
            # released and a retry can claim it fresh.
            if github_repo_for_agent and git_instance_id:
                try:
                    db.delete_git_config(config.name)
                except Exception as cleanup_exc:
                    logger.warning(
                        "Failed to roll back agent_git_config for %s after "
                        "creation failure: %s",
                        config.name,
                        cleanup_exc,
                    )
            logger.error(f"Failed to create agent {config.name}: {e}")
            raise HTTPException(status_code=500, detail="Failed to create agent. Please try again.")
    else:
        raise HTTPException(
            status_code=503,
            detail="Docker not available - cannot create agents in demo mode"
        )
