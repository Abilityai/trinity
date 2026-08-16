"""
Docker service for managing agent containers.
"""
import logging
import time
from typing import List, Optional, Set
import docker
from models import AgentStatus
from redis_breaker_util import get_breaker_redis
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
        base_image_version=base_image_version,
        ephemeral=labels.get("trinity.ephemeral") == "true",  # trinity-enterprise#69
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


def list_ephemeral_agent_containers() -> list:
    """Containers labeled ``trinity.ephemeral=true``, any state
    (trinity-enterprise#69).

    Used by the cleanup GC's Docker-as-truth orphan pass: an ephemeral
    container whose ownership row is gone (backend restarted mid-create or
    mid-discard) is reclaimable from the label alone. Returns raw container
    objects — callers read ``.labels`` (``trinity.agent-name``,
    ``trinity.created``) for the grace-window check.
    """
    if not docker_client:
        return []
    try:
        return docker_client.containers.list(
            all=True,
            filters={"label": "trinity.ephemeral=true"},
        )
    except Exception as e:
        logger.error(f"Error listing ephemeral agent containers: {e}")
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
                ephemeral=labels.get("trinity.ephemeral") == "true",  # trinity-enterprise#69
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
    """Check if a port is available on the host system.

    #2215 NB: this binds inside the BACKEND CONTAINER'S OWN network namespace.
    Agent SSH ports are host-published by Docker, so another agent's bind is
    invisible from here in production — this is a weak extra filter (it still
    catches backend-container-local binds), never the collision guard. The
    real guards are the label scan + the Redis reservation in
    `get_next_available_port`, plus the bind-conflict retry in crud (D2).
    """
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.bind(('0.0.0.0', port))
            return True
    except (socket.error, OSError):
        return False


# ---------------------------------------------------------------------------
# SSH-port allocation (#2215)
# ---------------------------------------------------------------------------

# Transient per-port reservation bridging the allocator's check and the
# caller's `containers.run`. Once the container exists, its `trinity.ssh-port`
# label is the durable truth (Invariant #11) — the reservation is never a port
# registry, so there is NO release path and NO refresh: TTL-only self-heal.
# crud allocates immediately before the container run (inside its rollback
# fence, after config staging / MCP-key mint / env build / the ent#15 snapshot
# prepopulate / volume builds), so the reserved window is essentially the
# `containers.run` itself plus the bounded bind-conflict retries; the other
# callers (`recreate_missing_container`, the system-agent bootstrap) run within
# a few statements of allocating. 600s is many-x headroom even over the
# observed 60s Docker read timeout; an expired reservation degrades to today's
# behaviour PLUS the crud bind-conflict retry (#2215 D2), and an orphaned
# reservation after a failed create idles one port of ~279 for 10 min.
# Keyspace note (#1560): `port_alloc:{port}` is deliberately NOT `agent:*` —
# the agent_runtime_state registry governs agent-NAME-keyed state cleared
# across the agent lifecycle; this key is port-keyed and self-expiring, and no
# lifecycle event should clear it (clearing a reservation on an agent event
# would un-reserve a port mid-create). Precedent: `ephemeral:quota:{owner_id}`.
_PORT_RESERVATION_TTL_SECONDS = 600
_PORT_RESERVATION_KEY_PREFIX = "port_alloc:"


def _existing_agent_ports_strict() -> Set[int]:
    """Agent SSH ports from container labels — RAISING on a Docker fault.

    Deliberately not `list_all_agents_fast()`: its #1131 swallow degrades a
    listing fault to `[]`, which here would make the allocator compute
    `start_port = 2222` and confidently reserve an existing agent's port —
    `is_port_available` is netns-blind in production, so nothing would catch
    it, and the bounded bind-retry (D2) can't outrun a whole fleet. A failed
    allocation is strictly better than a guaranteed collision, and every
    caller is about to talk to Docker anyway. `docker_client is None` (demo
    mode) still returns the empty set — crud's own 503 fires later, preserving
    demo behaviour. A malformed per-container label skips that container only.
    """
    if not docker_client:
        return set()
    try:
        containers = docker_client.containers.list(
            all=True,
            filters={"label": "trinity.platform=agent"},
        )
    except Exception as e:
        raise RuntimeError(
            f"cannot allocate a port: Docker listing failed ({e})"
        ) from e
    ports: Set[int] = set()
    for container in containers:
        try:
            port = int(container.labels.get("trinity.ssh-port", "0"))
        except Exception:
            continue
        if port:
            ports.add(port)
    return ports


def _try_reserve_port(client, port: int) -> bool:
    """`SET port_alloc:{port} "1" NX EX 600` — True reserved, False contended.

    Redis exceptions PROPAGATE; the caller decides fail-open. A dropped `ex`
    would make a stale key permanently unallocatable (no reaper exists), so
    the TTL rides on every reservation.
    """
    return bool(
        client.set(
            f"{_PORT_RESERVATION_KEY_PREFIX}{port}",
            "1",
            nx=True,
            ex=_PORT_RESERVATION_TTL_SECONDS,
        )
    )


def reserve_port_for_recreate(port: int) -> None:
    """Re-assert the reservation for a port a recreate is about to reuse (#2215).

    `recreate_container_with_updated_config` keeps the labeled port across its
    remove->create gap, during which the port is invisible to the allocator —
    and the most-recreated agent tends to hold the fleet's MAX port, exactly
    what a concurrent allocation computes as max+1. SET **without NX** (it is
    that agent's own port; a stale reservation must never block the recreate),
    same TTL. Fail-open, never raises — the reservation is a belt, not a gate.
    """
    try:
        client = get_breaker_redis()
        if client is None:
            return
        client.set(
            f"{_PORT_RESERVATION_KEY_PREFIX}{port}",
            "1",
            ex=_PORT_RESERVATION_TTL_SECONDS,
        )
    except Exception as e:
        logger.warning("Port %d recreate-reservation failed-open (%s)", port, e)


def get_next_available_port(exclude: Optional[Set[int]] = None) -> int:
    """Allocate the next SSH port for a new agent container.

    A candidate must pass, in order:
      1. not used by any existing Trinity agent container (STRICT label scan —
         a Docker listing fault raises instead of degrading to the empty set);
      2. not in ``exclude`` (ports the caller already tried and failed to bind);
      3. `is_port_available(port)` — weak, netns-blind extra filter (see above);
      4. the per-port Redis reservation (`port_alloc:{port}`, SETNX + 600s TTL)
         as the LAST gate, so reservations never leak onto candidates the scan
         walks past (a host-bound port must not hold a 600s reservation).

    Guarantee, stated precisely (#2215): with Redis up, two concurrent callers
    structurally cannot be ASSIGNED the same port — the SETNX is the atomic
    arbiter. With Redis down (every lock in this codebase deliberately fails
    open, and they share one client, so every guard degrades together), a
    same-port assignment is *convergent-under-retry*: it is detected at bind
    time and resolved within the same create call by crud's bounded
    bind-conflict retry (D2), which re-allocates with the failed ports
    excluded. Do NOT build on "atomic, unconditionally".

    SETNX contention (False) means a concurrent allocator holds that candidate
    — scan on to the next. Only a RAISED Redis error fails open: warn and
    return the current candidate unreserved (D2 converges any resulting
    collision). Redis-down cost is one ~1s bounded connect per call —
    `get_breaker_redis` pins 1s socket timeouts and caches only on success.
    """
    existing_ports = _existing_agent_ports_strict()
    # Merge ONCE, before both scan loops, so exclusion binds the forward scan
    # AND the 2222-2500 fallback scan.
    existing_ports |= exclude or set()

    redis_client = get_breaker_redis()  # resolved once per call; None => unreserved

    def _first_allocatable(candidates) -> Optional[int]:
        for port in candidates:
            if port in existing_ports or not is_port_available(port):
                continue
            if redis_client is None:
                return port
            try:
                if _try_reserve_port(redis_client, port):
                    return port
            except Exception as e:
                logger.warning(
                    "Port reservation failed-open (%s) — returning %d unreserved",
                    e,
                    port,
                )
                return port
            # SETNX contention: a concurrent allocator holds this candidate —
            # keep scanning.
        return None

    # Start from max existing port + 1, or 2222 if no agents exist
    start_port = max(existing_ports or {2221}) + 1

    # Try up to 100 ports to find an available one
    port = _first_allocatable(range(start_port, start_port + 100))
    if port is None:
        # Fallback: if all sequential ports are taken, scan from base
        port = _first_allocatable(range(2222, 2500))
    if port is None:
        raise RuntimeError("No available ports in range 2222-2500")
    return port


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
