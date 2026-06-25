"""
Docker service for managing agent containers.
"""
import logging
import time
from typing import List, Optional
import docker
from models import AgentStatus
from utils.helpers import parse_iso_timestamp, utc_now

logger = logging.getLogger(__name__)

# #1131: throttle the "socket access denied" WARN. The denial is a persistent
# condition (a wrong group_add GID stays wrong until the operator fixes .env),
# and list_all_agents_fast() is hit by the 5s heartbeat-watch and operator-queue
# loops, the 30s monitoring loop, and many request handlers — so an unthrottled
# warn floods Vector (~24+/min indefinitely). Log at most once per window so the
# failure stays observable without drowning the log stream. Module-level state;
# the GIL makes the read-modify-write atomic enough that a rare duplicate warn
# under a race is harmless.
_SOCKET_WARN_THROTTLE_S = 60.0
_last_socket_warn_monotonic: Optional[float] = None


# Initialize Docker client
try:
    docker_client = docker.from_env()
except Exception as e:
    print(f"Warning: Could not connect to Docker: {e}")
    docker_client = None


def get_agent_container(name: str):
    """Get agent container directly from Docker by name."""
    if not docker_client:
        return None
    try:
        container = docker_client.containers.get(f"agent-{name}")
        return container
    except docker.errors.NotFound:
        return None
    except Exception:
        return None


def get_agent_runtime(name: str) -> str:
    """Best-effort resolve an agent's execution runtime from its Docker label.

    Reads the ``trinity.agent-runtime`` label (written at create time by
    ``crud.py``). Used to make the platform system prompt runtime-aware (#1187
    F-MCP). Never raises and never blocks dispatch — any failure (no Docker
    client, container gone, missing label) falls back to ``"claude-code"``, which
    preserves the historical Claude/Gemini prompt naming.
    """
    container = get_agent_container(name)
    if container is None:
        return "claude-code"
    try:
        return container.labels.get("trinity.agent-runtime", "claude-code") or "claude-code"
    except Exception:
        return "claude-code"


def get_agent_status_from_container(container) -> AgentStatus:
    """Convert a Docker container to AgentStatus using container labels."""
    labels = container.labels
    # Use container name as authoritative source (handles rename correctly)
    # Container name is "agent-{name}", so strip the prefix
    agent_name = container.name.removeprefix("agent-")

    # Normalize Docker status to simpler values for frontend
    # Docker statuses: created, running, paused, restarting, removing, exited, dead
    docker_status = container.status
    if docker_status in ("exited", "dead", "created"):
        normalized_status = "stopped"
    elif docker_status == "running":
        normalized_status = "running"
    else:
        normalized_status = docker_status  # paused, restarting, etc.

    # Extract runtime from container environment variables
    runtime = "claude-code"  # Default
    try:
        # Get environment variables from container attrs
        env_list = container.attrs.get("Config", {}).get("Env", [])
        for env in env_list:
            if env.startswith("AGENT_RUNTIME="):
                runtime = env.split("=", 1)[1]
                break
    except Exception:
        pass  # Use default if we can't read env vars

    # Extract base image version from container labels or image labels
    base_image_version = labels.get("trinity.base-image-version")
    if not base_image_version:
        # Try to get from image labels
        try:
            image = container.image
            image_labels = image.labels or {}
            base_image_version = image_labels.get("trinity.base-image-version")
        except Exception:
            pass

    return AgentStatus(
        name=agent_name,
        type=labels.get("trinity.agent-type", "unknown"),
        status=normalized_status,
        port=int(labels.get("trinity.ssh-port", "0")),
        created=parse_iso_timestamp(labels["trinity.created"]) if labels.get("trinity.created") else utc_now(),
        resources={
            "cpu": labels.get("trinity.cpu", "2"),
            "memory": labels.get("trinity.memory", "4g")
        },
        container_id=container.id,
        template=labels.get("trinity.template", None) or None,
        runtime=runtime,
        base_image_version=base_image_version
    )


def list_all_agents() -> List[AgentStatus]:
    """List all Trinity agent containers from Docker."""
    if not docker_client:
        return []
    try:
        containers = docker_client.containers.list(
            all=True,
            filters={"label": "trinity.platform=agent"}
        )
        return [get_agent_status_from_container(c) for c in containers]
    except Exception as e:
        print(f"Error listing agents from Docker: {e}")
        return []


def list_all_agents_fast() -> List[AgentStatus]:
    """
    List all Trinity agent containers WITHOUT expensive Docker operations.

    This function extracts data ONLY from container labels and basic status,
    avoiding potentially slow operations like:
    - container.attrs (full inspect API call)
    - container.image (image metadata lookup)
    - container.stats() (CPU sampling - 2+ seconds per container)

    Use this for agent list endpoints where you need quick response times.
    Use list_all_agents() when you need full agent metadata.

    Performance: ~50ms for 10 agents vs ~2-3s with full metadata.
    """
    if not docker_client:
        return []
    try:
        containers = docker_client.containers.list(
            all=True,
            filters={"label": "trinity.platform=agent"}
        )

        agents = []
        for container in containers:
            labels = container.labels
            # Use container name as authoritative source (handles rename correctly)
            agent_name = container.name.removeprefix("agent-")

            # Normalize Docker status to simpler values for frontend
            docker_status = container.status
            if docker_status in ("exited", "dead", "created"):
                normalized_status = "stopped"
            elif docker_status == "running":
                normalized_status = "running"
            else:
                normalized_status = docker_status

            # Extract only data available in labels - no container.attrs or container.image
            agent = AgentStatus(
                name=agent_name,
                type=labels.get("trinity.agent-type", "unknown"),
                status=normalized_status,
                port=int(labels.get("trinity.ssh-port", "0")),
                created=parse_iso_timestamp(labels["trinity.created"]) if labels.get("trinity.created") else utc_now(),
                resources={
                    "cpu": labels.get("trinity.cpu", "2"),
                    "memory": labels.get("trinity.memory", "4g")
                },
                container_id=container.id,
                template=labels.get("trinity.template", None) or None,
                # Label written at create time is `trinity.agent-runtime` (crud.py);
                # `trinity.runtime` is never written, so reading it always reported
                # claude-code and broke the RuntimeBadge for Codex/Gemini agents
                # in every fast-path view (#1187 review I6).
                runtime=labels.get("trinity.agent-runtime", "claude-code"),
                base_image_version=labels.get("trinity.base-image-version"),  # Label only, no image lookup
            )
            agents.append(agent)

        return agents
    except Exception as e:
        # #1131: a denied docker.sock (e.g. wrong group_add GID on macOS Docker
        # Desktop) raises here; swallowing it silently showed "No agents" in the
        # UI with nothing in the logs. WARN so the failure is diagnosable — but
        # throttled (see _SOCKET_WARN_THROTTLE_S) so a persistent denial on this
        # per-poll hot path doesn't flood the logs. The broad catch can also see
        # transient daemon restarts / timeouts (which likewise repeat on this hot
        # path, so the throttle stays universal); the message stays generic rather
        # than asserting "socket access denied", and carries the exception text —
        # enough to identify the cause without exc_info.
        global _last_socket_warn_monotonic
        now = time.monotonic()
        if (
            _last_socket_warn_monotonic is None
            or now - _last_socket_warn_monotonic >= _SOCKET_WARN_THROTTLE_S
        ):
            _last_socket_warn_monotonic = now
            logger.warning("Failed to list agents from Docker: %s", e)
        return []


def get_agent_by_name(name: str) -> Optional[AgentStatus]:
    """Get a specific agent by name from Docker."""
    container = get_agent_container(name)
    if container:
        return get_agent_status_from_container(container)
    return None


def is_port_available(port: int) -> bool:
    """Check if a port is available on the host system."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.bind(('0.0.0.0', port))
            return True
    except (socket.error, OSError):
        return False


def get_next_available_port() -> int:
    """Get next available SSH port by checking existing containers and host availability.

    Finds the next port that is:
    1. Not used by any existing Trinity agent container
    2. Actually available on the host system (not bound by other processes)
    """
    agents = list_all_agents_fast()  # Only need port info from labels
    existing_ports = set(a.port for a in agents if a.port)

    # Start from max existing port + 1, or 2222 if no agents exist
    start_port = max(existing_ports or {2221}) + 1

    # Try up to 100 ports to find an available one
    for port in range(start_port, start_port + 100):
        if port not in existing_ports and is_port_available(port):
            return port

    # Fallback: if all sequential ports are taken, scan from base
    for port in range(2222, 2500):
        if port not in existing_ports and is_port_available(port):
            return port

    raise RuntimeError("No available ports in range 2222-2500")


async def execute_command_in_container(container_name: str, command: str, timeout: int = 60) -> dict:
    """Execute a command in a Docker container.

    Args:
        container_name: Name of the container (e.g., "agent-myagent")
        command: Command to execute
        timeout: Timeout in seconds

    Returns:
        Dictionary with 'exit_code' and 'output' keys
    """
    from services.docker_utils import container_exec_run, container_get

    if not docker_client:
        return {"exit_code": 1, "output": "Docker client not available"}

    try:
        container = await container_get(container_name)
        result = await container_exec_run(
            container,
            command,
            user="developer"
        )

        # result.exit_code is the exit code
        # result.output is bytes, decode to string
        output = result.output.decode('utf-8') if isinstance(result.output, bytes) else str(result.output)

        return {
            "exit_code": result.exit_code,
            "output": output
        }
    except docker.errors.NotFound:
        return {"exit_code": 1, "output": f"Container {container_name} not found"}
    except Exception as e:
        return {"exit_code": 1, "output": f"Error executing command: {str(e)}"}
