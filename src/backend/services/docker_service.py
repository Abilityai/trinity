"""
Docker service for managing agent containers.
"""
from typing import List, Optional
from datetime import datetime
import docker
from models import AgentStatus


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


def get_agent_status_from_container(container) -> AgentStatus:
    """Convert a Docker container to AgentStatus using container labels."""
    labels = container.labels
    agent_name = labels.get("trinity.agent-name", container.name.replace("agent-", ""))

    # Normalize Docker status to simpler values for frontend
    # Docker statuses: created, running, paused, restarting, removing, exited, dead
    docker_status = container.status
    if docker_status in ("exited", "dead", "created"):
        normalized_status = "stopped"
    elif docker_status == "running":
        normalized_status = "running"
    else:
        normalized_status = docker_status  # paused, restarting, etc.

    return AgentStatus(
        name=agent_name,
        type=labels.get("trinity.agent-type", "unknown"),
        status=normalized_status,
        port=int(labels.get("trinity.ssh-port", "0")),
        created=datetime.fromisoformat(labels.get("trinity.created", datetime.now().isoformat())),
        resources={
            "cpu": labels.get("trinity.cpu", "2"),
            "memory": labels.get("trinity.memory", "4g")
        },
        container_id=container.id,
        template=labels.get("trinity.template", None) or None
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
    agents = list_all_agents()
    existing_ports = set(a.port for a in agents if a.port)

    # Start from max existing port + 1, or 2290 if no agents exist
    start_port = max(existing_ports or {2289}) + 1

    # Try up to 100 ports to find an available one
    for port in range(start_port, start_port + 100):
        if port not in existing_ports and is_port_available(port):
            return port

    # Fallback: if all sequential ports are taken, scan from base
    for port in range(2290, 2500):
        if port not in existing_ports and is_port_available(port):
            return port

    raise RuntimeError("No available ports in range 2290-2500")
