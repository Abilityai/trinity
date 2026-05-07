"""
Agent Service Lifecycle - Agent start/stop and configuration management.

Contains functions for starting, stopping, and reconfiguring agents.
"""
import asyncio
import logging
import os
import time

import docker
import httpx

from fastapi import HTTPException

from database import db
from services.docker_service import (
    docker_client,
    get_agent_container,
)
from services.docker_utils import (
    container_stop, container_remove, container_start, container_reload,
    volume_get, volume_create, containers_run
)
from services.agent_service.helpers import validate_base_image
from services.settings_service import get_anthropic_api_key, get_github_pat, get_agent_full_capabilities, get_agent_default_resources
from services.skill_service import skill_service
from .helpers import check_shared_folder_mounts_match, check_api_key_env_matches, check_github_pat_env_matches, check_resource_limits_match, check_full_capabilities_match, check_guardrails_env_matches
from .file_sharing import check_public_folder_mount_matches
from .read_only import inject_read_only_hooks

logger = logging.getLogger(__name__)


# =============================================================================
# Readiness Probe (#406)
# =============================================================================

# Docker reporting a container as "running" precedes the in-container FastAPI
# server accepting connections by several seconds. Under multi-agent deploys,
# the downstream credential-injection retry window exhausts before the server
# is up. Gate post-start injections on HTTP readiness to close the race.

AGENT_READINESS_TIMEOUT_S = int(os.getenv("AGENT_READINESS_TIMEOUT_S", "60"))
AGENT_READINESS_POLL_INTERVAL_S = float(os.getenv("AGENT_READINESS_POLL_INTERVAL_S", "1.0"))


async def wait_for_agent_ready(
    agent_name: str,
    timeout_s: int = AGENT_READINESS_TIMEOUT_S,
    poll_interval_s: float = AGENT_READINESS_POLL_INTERVAL_S,
) -> bool:
    """Poll the agent's /health endpoint until it returns 200 or timeout.

    Returns True if ready, False on timeout. Never raises — callers treat a
    False return as "proceed anyway and let downstream retries cope."
    """
    url = f"http://agent-{agent_name}:8000/health"
    deadline = time.monotonic() + timeout_s
    attempt = 0
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            attempt += 1
            try:
                r = await client.get(url, timeout=2.0)
                if r.status_code == 200:
                    if attempt > 1:
                        logger.info(
                            f"Agent {agent_name} became ready after {attempt} poll(s)"
                        )
                    return True
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                pass
            except Exception as e:  # noqa: BLE001 — readiness probe must never bubble
                logger.debug(
                    f"Readiness probe for {agent_name} hit unexpected error: {e}"
                )
            await asyncio.sleep(poll_interval_s)

    logger.warning(
        f"Agent {agent_name} did not become ready within {timeout_s}s "
        f"(polled {attempt} time(s)) — proceeding anyway"
    )
    return False


# =============================================================================
# Container Security Capability Sets
# =============================================================================
# These define the Linux capabilities granted to agent containers.
# Security principle: Always drop ALL caps, then add back only what's needed.

# Restricted mode capabilities - minimum for agent operation (default)
RESTRICTED_CAPABILITIES = [
    'NET_BIND_SERVICE',  # Bind to ports < 1024
    'SETGID', 'SETUID',  # Change user/group (for su/sudo)
    'CHOWN',             # Change file ownership
    'SYS_CHROOT',        # Use chroot
    'AUDIT_WRITE',       # Write to audit log
]

# Full capabilities mode - adds package installation support
# Used when agents need apt-get, pip install, etc.
FULL_CAPABILITIES = RESTRICTED_CAPABILITIES + [
    'DAC_OVERRIDE',      # Bypass file permission checks (needed for apt)
    'FOWNER',            # Bypass permission checks on file owner
    'FSETID',            # Don't clear setuid/setgid bits
    'KILL',              # Send signals to processes
    'MKNOD',             # Create special files
    'NET_RAW',           # Use raw sockets (ping, etc.)
    'SYS_PTRACE',        # Trace processes (debugging)
]

# These capabilities are NEVER granted - they pose significant security risks
# Listed for documentation; we achieve this by always using cap_drop=['ALL']
PROHIBITED_CAPABILITIES = [
    'SYS_ADMIN',         # Mount filesystems, configure namespace - too powerful
    'NET_ADMIN',         # Network administration - could escape container
    'SYS_RAWIO',         # Raw I/O access - direct hardware access
    'SYS_MODULE',        # Load kernel modules - kernel compromise
    'SYS_BOOT',          # Reboot system
]


async def inject_assigned_credentials(agent_name: str, max_retries: int = 3, retry_delay: float = 2.0) -> dict:
    """
    Import credentials from encrypted .credentials.enc file on agent startup.

    CRED-002: Credentials are now stored as encrypted files in the agent's
    workspace (committed to git). On startup, we try to import from
    .credentials.enc if it exists.

    Args:
        agent_name: Name of the agent
        max_retries: Number of retries for connection
        retry_delay: Seconds between retries

    Returns:
        dict with injection status
    """
    import asyncio
    from database import db
    from services.credential_encryption import (
        CredentialsFileNotFoundError,
        get_credential_encryption_service,
    )

    # #612: subscription-mode agents authenticate via CLAUDE_CODE_OAUTH_TOKEN
    # env var set at container creation (SUB-002). They do not need (and
    # typically do not have) a .credentials.enc file. Attempting the import
    # would either silently succeed-noop or surface a misleading "failed"
    # status that prompts operators to take corrective action (re-assigning
    # the subscription, recreating the container) — when nothing is wrong.
    # Short-circuit to a clear skipped status before the import path runs.
    if db.get_agent_subscription_id(agent_name):
        logger.debug(
            f"Skipping .credentials.enc import for {agent_name}: "
            f"subscription mode (auth via CLAUDE_CODE_OAUTH_TOKEN env var)"
        )
        return {
            "status": "skipped",
            "reason": "subscription_mode",
            "detail": "agent authenticates via CLAUDE_CODE_OAUTH_TOKEN; "
                      "file-based credential injection is not used",
        }

    try:
        encryption_service = get_credential_encryption_service()
    except ValueError as e:
        # No encryption key configured - this is optional
        logger.debug(f"Credential encryption not configured: {e}")
        return {"status": "skipped", "reason": "encryption_not_configured"}

    # Try to import from .credentials.enc with retries
    last_error = None
    for attempt in range(max_retries):
        try:
            files = await encryption_service.import_to_agent(agent_name)
            if files:
                logger.info(f"Imported {len(files)} credential file(s) from .credentials.enc into {agent_name}")
                return {
                    "status": "success",
                    "credential_count": len(files),
                    "files": list(files.keys())
                }
            else:
                return {"status": "skipped", "reason": "no_credentials_enc_file"}

        except CredentialsFileNotFoundError:
            # #612: ``.credentials.enc`` is absent. Common case for fresh
            # agents that haven't been through an export cycle yet — a clean
            # skip, not a failure. (Was previously caught by a fragile
            # substring match against the error message; the explicit
            # subclass makes the intent unambiguous.)
            logger.debug(f"No .credentials.enc found for agent {agent_name}")
            return {"status": "skipped", "reason": "no_credentials_enc_file"}

        except ValueError as e:
            # Other ValueError shapes (encrypted blob malformed, decrypt
            # failure, …) — keep retrying because some of them are
            # transient (e.g. agent HTTP not yet ready under multi-agent
            # cold start, #406).
            last_error = str(e)

        except Exception as e:
            last_error = str(e)
            logger.warning(f"Credential import attempt {attempt + 1} failed: {last_error}")

        if attempt < max_retries - 1:
            await asyncio.sleep(retry_delay)

    logger.error(f"Failed to import credentials into agent {agent_name} after {max_retries} attempts: {last_error}")
    return {"status": "failed", "error": last_error}


async def inject_assigned_skills(agent_name: str) -> dict:
    """
    Inject assigned skills into a running agent.

    This is called after agent startup to push any skills that were
    assigned to this agent in the Skills tab.

    Args:
        agent_name: Name of the agent

    Returns:
        dict with injection status
    """
    from database import db

    # Get assigned skills
    skill_names = db.get_agent_skill_names(agent_name)

    if not skill_names:
        logger.debug(f"No assigned skills for agent {agent_name}")
        return {"status": "skipped", "reason": "no_skills"}

    logger.info(f"Injecting {len(skill_names)} skills into agent {agent_name}: {skill_names}")

    # Inject skills
    result = await skill_service.inject_skills(agent_name, skill_names)

    if result.get("success"):
        return {
            "status": "success",
            "skills_injected": result.get("skills_injected", 0)
        }
    else:
        return {
            "status": "partial" if result.get("skills_injected", 0) > 0 else "failed",
            "skills_injected": result.get("skills_injected", 0),
            "skills_failed": result.get("skills_failed", 0),
            "results": result.get("results", {})
        }


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

    # Check if container needs recreation for shared folders, API key, resource limits, or capabilities
    await container_reload(container)
    was_already_running = getattr(container, "status", None) == "running"
    shared_folder_match = await check_shared_folder_mounts_match(container, agent_name)
    needs_recreation = (
        not shared_folder_match or
        not check_public_folder_mount_matches(container, agent_name) or
        not check_api_key_env_matches(container, agent_name) or
        not check_github_pat_env_matches(container, agent_name) or
        not check_resource_limits_match(container, agent_name) or
        not check_full_capabilities_match(container, agent_name) or
        not check_guardrails_env_matches(container, agent_name)
    )

    if needs_recreation:
        # Recreate container with updated config
        # Use system user for internal operations
        await recreate_container_with_updated_config(agent_name, container, "system")
        container = get_agent_container(agent_name)

    await container_start(container)

    # NOTE: Trinity platform instructions are now injected at runtime via
    # --append-system-prompt on every chat/task request (Issue #136).
    # No file-based injection needed on startup.

    # Skip credential/skill injection when the container was already running
    # and we didn't recreate it (#421). The workspace volume persists `.env`
    # and `.claude/skills/` across container starts, so re-injection on an
    # idempotent start is redundant and generates connection-error noise when
    # the agent is under load and can't accept new HTTP connections.
    skip_injection = was_already_running and not needs_recreation

    if skip_injection:
        credentials_result = {
            "status": "skipped",
            "reason": "container_already_running",
        }
        credentials_status = "skipped"
        skills_result = {
            "status": "skipped",
            "reason": "container_already_running",
        }
        skills_status = "skipped"
    else:
        # Gate post-start injections on HTTP readiness — Docker "running"
        # precedes FastAPI "listening" by several seconds, and the downstream
        # retry window is too short under multi-agent deploys (#406).
        await wait_for_agent_ready(agent_name)

        # Inject assigned credentials from the Credentials page
        credentials_result = await inject_assigned_credentials(agent_name)
        credentials_status = credentials_result.get("status", "unknown")

        # Inject assigned skills from the Skills page
        skills_result = await inject_assigned_skills(agent_name)
        skills_status = skills_result.get("status", "unknown")

    # Inject read-only hooks if enabled
    read_only_result = {"status": "skipped", "reason": "not_enabled"}
    read_only_data = db.get_read_only_mode(agent_name)
    if read_only_data.get("enabled"):
        try:
            read_only_result = await inject_read_only_hooks(agent_name, read_only_data.get("config"))
            if read_only_result.get("success"):
                read_only_result["status"] = "success"
            else:
                read_only_result["status"] = "failed"
        except Exception as e:
            logger.warning(f"Failed to inject read-only hooks into agent {agent_name}: {e}")
            read_only_result = {"status": "failed", "error": str(e)}

    return {
        "message": f"Agent {agent_name} started",
        "credentials_injection": credentials_status,
        "credentials_result": credentials_result,
        "skills_injection": skills_status,
        "skills_result": skills_result,
        "read_only_injection": read_only_result.get("status", "unknown"),
        "read_only_result": read_only_result
    }


async def recreate_container_with_updated_config(agent_name: str, old_container, owner_username: str):
    """
    Recreate an agent container with updated configuration.
    Handles shared folder mounts and API key settings.
    Preserves the agent's workspace volume and other configuration.
    """
    # Extract configuration from old container
    old_config = old_container.attrs.get("Config", {})
    old_host_config = old_container.attrs.get("HostConfig", {})

    # Get key settings
    image = old_config.get("Image", "trinity-agent-base:latest")
    # SEC-172: Validate image on container recreation (defense in depth)
    validate_base_image(image)
    env_vars = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in old_config.get("Env", []) if "=" in e}
    labels = old_config.get("Labels", {})

    # Update auth env vars based on current setting (SUB-002).
    # Claude Code prioritizes ANTHROPIC_API_KEY over CLAUDE_CODE_OAUTH_TOKEN,
    # so when a subscription is assigned we must remove the API key and set
    # the token env var instead.
    subscription_id = db.get_agent_subscription_id(agent_name)
    has_subscription = subscription_id is not None
    use_platform_key = db.get_use_platform_api_key(agent_name)

    if has_subscription:
        # Subscription assigned — inject token, remove API key
        token = db.get_subscription_token(subscription_id)
        if token:
            env_vars['CLAUDE_CODE_OAUTH_TOKEN'] = token
        env_vars.pop('ANTHROPIC_API_KEY', None)
    elif use_platform_key:
        # No subscription, use platform API key
        env_vars['ANTHROPIC_API_KEY'] = get_anthropic_api_key()
        env_vars.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
    else:
        # No subscription, no platform key — user will auth in terminal
        env_vars.pop('ANTHROPIC_API_KEY', None)
        env_vars.pop('CLAUDE_CODE_OAUTH_TOKEN', None)

    # Update GITHUB_PAT if the container has one and a current system PAT exists
    if env_vars.get('GITHUB_PAT'):
        current_pat = get_github_pat()
        if current_pat:
            env_vars['GITHUB_PAT'] = current_pat

    # GUARD-001: re-serialise guardrails overrides into env so startup.sh
    # can render the runtime config with the latest values.
    guardrails_override = db.get_guardrails_config(agent_name)
    if guardrails_override:
        import json as _json
        env_vars['AGENT_GUARDRAILS'] = _json.dumps(guardrails_override)
    else:
        env_vars.pop('AGENT_GUARDRAILS', None)

    # Get port from labels
    ssh_port = int(labels.get("trinity.ssh-port", 2222))

    # Get resource limits: per-agent DB override → container labels → system defaults → hardcoded
    db_limits = db.get_resource_limits(agent_name)
    system_defaults = get_agent_default_resources()
    if db_limits:
        cpu = db_limits.get("cpu") or labels.get("trinity.cpu") or system_defaults["cpu"]
        memory = db_limits.get("memory") or labels.get("trinity.memory") or system_defaults["memory"]
    else:
        cpu = labels.get("trinity.cpu") or system_defaults["cpu"]
        memory = labels.get("trinity.memory") or system_defaults["memory"]

    # Update labels with new resource limits for future reference
    labels["trinity.cpu"] = cpu
    labels["trinity.memory"] = memory

    # Get full_capabilities from system-wide setting (not per-agent)
    full_capabilities = get_agent_full_capabilities()

    # Update label to reflect current setting
    labels["trinity.full-capabilities"] = str(full_capabilities).lower()

    # Stop and remove old container
    try:
        await container_stop(old_container)
    except Exception:
        pass
    await container_remove(old_container)

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
        # Skip public mount — re-added below based on current file_sharing_enabled flag.
        if dest == db.get_public_mount_path():
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
                await volume_get(shared_volume_name)
            except docker.errors.NotFound:
                await volume_create(
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
                    await containers_run(
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
                    await volume_get(source_volume)
                    volumes[source_volume] = {'bind': mount_path, 'mode': 'rw'}
                except docker.errors.NotFound:
                    pass

    # Add public folder mount based on current file_sharing_enabled flag
    # (FILES-001 Step 2). Mirrors the shared-folders expose pattern.
    if db.get_file_sharing_enabled(agent_name):
        public_volume_name = db.get_public_volume_name(agent_name)
        public_volume_created = False
        try:
            await volume_get(public_volume_name)
        except docker.errors.NotFound:
            await volume_create(
                name=public_volume_name,
                labels={
                    'trinity.platform': 'agent-public',
                    'trinity.agent-name': agent_name,
                },
            )
            public_volume_created = True

        if public_volume_created:
            try:
                await containers_run(
                    'alpine',
                    command='chown 1000:1000 /public',
                    volumes={public_volume_name: {'bind': '/public', 'mode': 'rw'}},
                    remove=True,
                )
            except Exception as e:
                logger.warning(f"Could not fix public volume ownership: {e}")

        volumes[public_volume_name] = {'bind': db.get_public_mount_path(), 'mode': 'rw'}

    # Create new container with security settings
    # Security principle: ALWAYS apply baseline security, even in full_capabilities mode
    # - Always drop ALL caps, then add back only what's needed
    # - Always apply AppArmor profile
    # - Always apply noexec,nosuid to /tmp
    new_container = await containers_run(
        image,
        detach=True,
        name=f"agent-{agent_name}",
        ports={'22/tcp': ssh_port},
        volumes=volumes,
        environment=env_vars,
        labels=labels,
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
        mem_limit=memory,
        cpu_count=int(cpu)
    )

    logger.info(f"Recreated container for agent {agent_name} with updated configuration")
    return new_container
