"""
Agent Service Lifecycle - Agent start/stop and configuration management.

Contains functions for starting, stopping, and reconfiguring agents.
"""
import logging
import asyncio
import docker

from fastapi import HTTPException

from database import db
from services.docker_service import (
    docker_client,
    get_agent_container,
)
from routers.settings import get_anthropic_api_key
from .helpers import check_shared_folder_mounts_match, check_api_key_env_matches

logger = logging.getLogger(__name__)


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
        not check_shared_folder_mounts_match(container, agent_name) or
        not check_api_key_env_matches(container, agent_name)
    )

    if needs_recreation:
        # Recreate container with updated config
        # Use system user for internal operations
        await recreate_container_with_updated_config(agent_name, container, "system")
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

    logger.info(f"Recreated container for agent {agent_name} with updated configuration")
    return new_container
