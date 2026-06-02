"""
Async-safe Docker operations.

Wraps blocking Docker SDK calls in ThreadPoolExecutor to prevent
event loop blocking. All Docker operations in async contexts MUST
use these functions instead of calling the Docker SDK directly.

Reference: src/backend/routers/telemetry.py (existing correct pattern)
Issue: https://github.com/abilityai/trinity/issues/42
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from services.docker_service import docker_client

logger = logging.getLogger(__name__)

# Shared executor - limited to 4 workers to avoid overwhelming Docker daemon
# This matches the pattern in telemetry.py
_docker_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="docker-")


def _invalidate_agent_stats_for(container) -> None:
    """#73: drop the per-agent live-stats cache for this container's agent.

    docker_utils is the chokepoint for nearly every container lifecycle op
    (stop/start/remove/rename), so invalidating here — rather than in
    individual router handlers — drops the stale stats entry immediately for
    the paths that go through these primitives (UI, ops restart, deploy,
    subscription re-assign, system restart) instead of waiting out the cache
    TTL. The one deliberate exception is the Operating Room emergency-stop fast
    path (routers/ops.py:_stop_agent_container), which calls container.stop()
    directly in a thread pool for parallel shutdown and so relies on the cache
    TTL (<=12s) rather than explicit invalidation — an accepted bound on an
    already-coarse gauge.

    Best-effort by design:
    - a lazy import breaks the docker_utils -> agent_service import cycle
      (agent_service modules import docker_utils);
    - the agent name is read from the `trinity.agent-name` label, falling back
      to the `agent-{name}` container-name convention; non-agent containers
      yield None and are skipped;
    - any failure is logged and swallowed — cache invalidation must never break
      the container operation it follows.
    """
    try:
        labels = getattr(container, "labels", None) or {}
        agent_name = labels.get("trinity.agent-name")
        if not agent_name:
            cname = getattr(container, "name", "") or ""
            if cname.startswith("agent-"):
                agent_name = cname[len("agent-"):]
        if not agent_name:
            return
        # Lazy import: docker_utils <- agent_service would otherwise be circular.
        from services.agent_service.stats import invalidate_agent_stats_cache
        invalidate_agent_stats_cache(agent_name)
    except Exception as exc:  # never let cache invalidation break a lifecycle op
        # warning (not debug): a per-call miss is harmless, but a SYSTEMATIC
        # failure here (e.g. the lazy import regressing) would silently no-op
        # every invalidation fleet-wide, visible only as <=TTL stale stats.
        logger.warning("stats-cache invalidation skipped: %s", exc)


# =============================================================================
# Container Operations
# =============================================================================

async def container_stop(container, timeout: int = 10) -> None:
    """Stop a container without blocking the event loop.

    Args:
        container: Docker container object
        timeout: Seconds to wait before killing (default 10)
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_docker_executor, lambda: container.stop(timeout=timeout))
    _invalidate_agent_stats_for(container)  # #73


async def container_remove(container, force: bool = False) -> None:
    """Remove a container without blocking the event loop.

    Args:
        container: Docker container object
        force: Force removal even if running
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_docker_executor, lambda: container.remove(force=force))
    _invalidate_agent_stats_for(container)  # #73


async def container_start(container) -> None:
    """Start a container without blocking the event loop.

    Args:
        container: Docker container object
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_docker_executor, container.start)
    _invalidate_agent_stats_for(container)  # #73


async def container_reload(container) -> None:
    """Reload container attributes without blocking the event loop.

    Args:
        container: Docker container object
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_docker_executor, container.reload)


async def container_stats(container, stream: bool = False) -> Dict[str, Any]:
    """Get container stats without blocking the event loop.

    Args:
        container: Docker container object
        stream: If False, return single stats snapshot (default False)
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _docker_executor,
        lambda: container.stats(stream=stream)
    )


async def container_rename(container, new_name: str) -> None:
    """Rename a container without blocking the event loop.

    Args:
        container: Docker container object
        new_name: New name for the container
    """
    # #73: capture the OLD agent identity BEFORE the rename — the freed name is
    # what must be evicted (a reused name must not serve the renamed-away
    # agent's stale stats). The container's label/name still reflect the old
    # value here.
    _invalidate_agent_stats_for(container)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_docker_executor, lambda: container.rename(new_name))


async def container_get(container_id: str) -> Any:
    """Get a container by ID/name without blocking the event loop.

    Args:
        container_id: Container ID or name

    Returns:
        Container object

    Raises:
        docker.errors.NotFound: If container doesn't exist
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _docker_executor,
        docker_client.containers.get,
        container_id
    )


# =============================================================================
# Volume Operations
# =============================================================================

async def volume_get(name: str) -> Any:
    """Get a volume without blocking the event loop.

    Args:
        name: Volume name

    Returns:
        Volume object

    Raises:
        docker.errors.NotFound: If volume doesn't exist
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_docker_executor, docker_client.volumes.get, name)


async def volume_create(name: str, labels: Optional[Dict[str, str]] = None) -> Any:
    """Create a volume without blocking the event loop.

    Args:
        name: Volume name
        labels: Optional labels dict

    Returns:
        Created volume object
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _docker_executor,
        lambda: docker_client.volumes.create(name=name, labels=labels or {})
    )


async def volume_remove(volume) -> None:
    """Remove a volume without blocking the event loop.

    Args:
        volume: Volume object
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_docker_executor, volume.remove)


# =============================================================================
# Container Creation (Complex)
# =============================================================================

async def containers_run(
    image: str,
    command: Optional[str] = None,
    **kwargs
) -> Any:
    """
    Run a container without blocking the event loop.

    Accepts all docker-py containers.run() kwargs.

    Args:
        image: Image name
        command: Optional command to run
        **kwargs: All other containers.run() parameters

    Returns:
        Container object (if detach=True) or logs
    """
    loop = asyncio.get_event_loop()

    def _run():
        return docker_client.containers.run(image, command=command, **kwargs)

    return await loop.run_in_executor(_docker_executor, _run)


# =============================================================================
# Container Exec Operations
# =============================================================================

async def container_exec_run(
    container,
    cmd: str,
    user: str = None,
    workdir: str = None,
    environment: Dict[str, str] = None
) -> Any:
    """Execute a command in a container without blocking the event loop.

    Args:
        container: Docker container object
        cmd: Command to execute
        user: Optional user to run as
        workdir: Optional working directory
        environment: Optional environment variables

    Returns:
        ExecResult with exit_code and output
    """
    loop = asyncio.get_event_loop()

    def _exec():
        kwargs = {}
        if user:
            kwargs['user'] = user
        if workdir:
            kwargs['workdir'] = workdir
        if environment:
            kwargs['environment'] = environment
        return container.exec_run(cmd, **kwargs)

    return await loop.run_in_executor(_docker_executor, _exec)


async def container_get_archive(container, path: str) -> tuple:
    """Read a tar archive out of a container without blocking the event loop.

    Args:
        container: Docker container object
        path: Source path inside the container

    Returns:
        (stream_generator, stat_dict) matching docker-py's return shape.
        Raises docker.errors.NotFound when the path doesn't exist.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _docker_executor,
        lambda: container.get_archive(path),
    )


async def container_put_archive(container, path: str, data: bytes) -> bool:
    """Write a tar archive into a container without blocking the event loop.

    Args:
        container: Docker container object
        path: Destination directory inside the container
        data: tar archive bytes (use tarfile to create)

    Returns:
        True on success, False on failure.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _docker_executor,
        lambda: container.put_archive(path, data)
    )


async def api_exec_create(
    container_id: str,
    cmd: list,
    stdin: bool = True,
    tty: bool = True,
    stdout: bool = True,
    stderr: bool = True,
    user: str = None,
    workdir: str = None,
    environment: Dict[str, str] = None
) -> Dict[str, Any]:
    """Create an exec instance using Docker API without blocking.

    Args:
        container_id: Container ID
        cmd: Command as list of strings
        stdin: Attach stdin
        tty: Allocate TTY
        stdout: Attach stdout
        stderr: Attach stderr
        user: Optional user
        workdir: Optional working directory
        environment: Optional environment dict

    Returns:
        Exec instance dict with 'Id' key
    """
    loop = asyncio.get_event_loop()

    def _create():
        return docker_client.api.exec_create(
            container_id,
            cmd,
            stdin=stdin,
            tty=tty,
            stdout=stdout,
            stderr=stderr,
            user=user,
            workdir=workdir,
            environment=environment
        )

    return await loop.run_in_executor(_docker_executor, _create)


async def api_exec_start(exec_id: str, socket: bool = False, tty: bool = True) -> Any:
    """Start an exec instance using Docker API without blocking.

    Args:
        exec_id: Exec instance ID
        socket: Return socket for bidirectional communication
        tty: Use TTY mode

    Returns:
        Socket object (if socket=True) or output
    """
    loop = asyncio.get_event_loop()

    def _start():
        return docker_client.api.exec_start(exec_id, socket=socket, tty=tty)

    return await loop.run_in_executor(_docker_executor, _start)


# =============================================================================
# Container Listing (Already optimized in docker_service.py)
# =============================================================================
# Note: list_all_agents() and list_all_agents_fast() are already efficient
# and don't require async wrappers as they complete quickly (<50ms).
# Only wrap if profiling shows they become a bottleneck.
