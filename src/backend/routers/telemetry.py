"""
Host and Container Telemetry Endpoints

Provides real-time system metrics for the Dashboard:
- Host system stats (CPU, memory, disk)
- Aggregate container stats across all running agents

Container stats are served from a short-TTL, background-refreshed cache so the
request path NEVER blocks on the Docker daemon (#1096). `container.stats()` is
a ~1-2s-per-container call, so a fleet of N agents made the endpoint take
`ceil(N/pool) * ~1.5s` — ~6s at 11 agents — pinning a uvicorn worker for the
whole duration and starving the rest of the UI. The handler now does
stale-while-revalidate: it returns whatever is cached immediately and schedules
an out-of-band refresh; the expensive Docker work is fully decoupled from the
HTTP request.
"""

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional

import psutil
from fastapi import APIRouter, Depends, HTTPException

from dependencies import get_current_user
from models import User
from services.docker_service import docker_client, list_all_agents_fast
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration (env-overridable, defensively parsed so a bad value can never
# crash the router at import time — it falls back to the default).
# ---------------------------------------------------------------------------
_DEFAULT_CACHE_TTL = 10.0   # seconds a cached container-stats payload is "fresh"
_DEFAULT_POOL_SIZE = 16     # max concurrent Docker stat fetches on the refresh
_POOL_SIZE_MAX = 64


def _parse_float_env(value: Optional[str], default: float, *, minimum: float = 0.0) -> float:
    """Parse a float env var, falling back to `default` on anything invalid.

    Negative / zero is clamped to `minimum` (0 disables the cache → every
    request schedules a refresh and serves stale, never blocking)."""
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else minimum


def _parse_pool_size(value: Optional[str], default: int) -> int:
    """Parse the Docker stat pool size; clamp to [1, _POOL_SIZE_MAX]."""
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, _POOL_SIZE_MAX))


_CACHE_TTL = _parse_float_env(os.getenv("TELEMETRY_CONTAINER_STATS_TTL"), _DEFAULT_CACHE_TTL)
_POOL_SIZE = _parse_pool_size(os.getenv("TELEMETRY_DOCKER_POOL_SIZE"), _DEFAULT_POOL_SIZE)

# Module-level executor for the blocking Docker stat fetches. Sized larger than
# the old hard cap of 4 because the work now runs on a background task off the
# request path, so a cold refresh is ~one Docker-sample window instead of
# ceil(N/4) windows. Threads idle cheaply when there is no refresh in flight.
_docker_executor = ThreadPoolExecutor(
    max_workers=_POOL_SIZE, thread_name_prefix="telemetry-docker"
)

# Background-refreshed cache. `data` is the last fully-computed payload (or
# None before the first successful refresh); `timestamp` is the monotonic time
# of that compute. Single-process state — each uvicorn worker keeps its own.
_stats_cache: Dict[str, Any] = {"timestamp": 0.0, "data": None}

# Strong reference to the in-flight refresh task so (a) we never launch two at
# once and (b) the fire-and-forget task can't be garbage-collected mid-run.
_refresh_task: Optional["asyncio.Task"] = None

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])

# Initialize CPU percent tracking (first call with interval=None returns 0)
# This primes the counter so subsequent calls return meaningful values
psutil.cpu_percent(interval=None)


@router.get("/host")
async def get_host_stats(current_user: User = Depends(get_current_user)):
    """
    Get host system statistics using psutil.

    Returns CPU, memory, and disk usage metrics.
    Requires authentication (SEC-180).
    """
    try:
        # CPU - use interval=None to get last computed value (non-blocking)
        cpu_percent = psutil.cpu_percent(interval=None)
        cpu_count = psutil.cpu_count()

        # Memory
        mem = psutil.virtual_memory()

        # Disk - use root partition
        disk = psutil.disk_usage('/')

        return {
            "cpu": {
                "percent": round(cpu_percent, 1),
                "count": cpu_count
            },
            "memory": {
                "percent": round(mem.percent, 1),
                "used_gb": round(mem.used / (1024**3), 1),
                "total_gb": round(mem.total / (1024**3), 1)
            },
            "disk": {
                "percent": round(disk.percent, 1),
                "used_gb": round(disk.used / (1024**3), 1),
                "total_gb": round(disk.total / (1024**3), 1)
            },
            "timestamp": utc_now_iso()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting host stats: {str(e)}")


def _get_single_container_stats_sync(agent_name: str) -> Dict[str, Any]:
    """
    Synchronous helper to get stats for a single container.
    Runs in thread pool to avoid blocking the event loop.
    """
    try:
        container = docker_client.containers.get(f"agent-{agent_name}")

        # Get stats (one-shot) - this is the blocking call (~1-2s per container)
        stats = container.stats(stream=False)

        # Calculate CPU percentage
        cpu_percent = 0.0
        cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - \
                   stats['precpu_stats']['cpu_usage']['total_usage']
        system_delta = stats['cpu_stats']['system_cpu_usage'] - \
                      stats['precpu_stats']['system_cpu_usage']

        if system_delta > 0 and cpu_delta > 0:
            num_cpus = len(stats['cpu_stats']['cpu_usage'].get('percpu_usage', [])) or 1
            cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0

        # Get memory usage (subtract cache for accuracy)
        memory_usage = stats['memory_stats'].get('usage', 0)
        memory_cache = stats['memory_stats'].get('stats', {}).get('cache', 0)
        memory_used = memory_usage - memory_cache
        memory_mb = round(memory_used / (1024 * 1024), 1)

        return {
            "name": agent_name,
            "cpu": round(cpu_percent, 1),
            "memory_mb": memory_mb
        }

    except Exception as e:
        return {
            "name": agent_name,
            "cpu": 0,
            "memory_mb": 0,
            "error": str(e)
        }


def _empty_payload(running_count: int = 0) -> Dict[str, Any]:
    """A fully-shaped container-stats payload with no per-container data."""
    return {
        "running_count": running_count,
        "total_cpu_percent": 0,
        "total_memory_mb": 0,
        "containers": [],
        "timestamp": utc_now_iso(),
    }


async def _compute_container_stats() -> Dict[str, Any]:
    """Do the expensive work: list running agents and fetch each container's
    Docker stats in parallel. Returns a fully-shaped payload. This is what the
    background refresh runs — it must never touch the request path directly."""
    loop = asyncio.get_running_loop()

    # Off-load even the agent listing (a Docker call) to the executor so the
    # event loop is never blocked during a refresh.
    agents = await loop.run_in_executor(_docker_executor, list_all_agents_fast)
    running_agents = [a for a in agents if a.status == "running"]

    if not running_agents:
        return _empty_payload(0)

    tasks = [
        loop.run_in_executor(_docker_executor, _get_single_container_stats_sync, agent.name)
        for agent in running_agents
    ]
    containers_stats = await asyncio.gather(*tasks, return_exceptions=True)

    processed_stats: List[Dict[str, Any]] = []
    total_cpu_percent = 0.0
    total_memory_mb = 0.0

    for result in containers_stats:
        if isinstance(result, dict):
            processed_stats.append(result)
            if "error" not in result:
                total_cpu_percent += result.get("cpu", 0)
                total_memory_mb += result.get("memory_mb", 0)

    return {
        "running_count": len(running_agents),
        "total_cpu_percent": round(total_cpu_percent, 1),
        "total_memory_mb": round(total_memory_mb, 1),
        "containers": sorted(processed_stats, key=lambda x: x.get('cpu', 0), reverse=True),
        "timestamp": utc_now_iso(),
    }


async def _refresh_cache() -> None:
    """Recompute the container-stats cache. On failure the previous cache is
    left untouched (best-effort metrics — never surface a Docker hiccup as a
    request error)."""
    try:
        payload = await _compute_container_stats()
        _stats_cache["data"] = payload
        _stats_cache["timestamp"] = time.monotonic()
    except Exception as e:
        logger.warning("telemetry container-stats refresh failed: %s", e)


def _schedule_refresh() -> None:
    """Launch a background refresh unless one is already in flight. Race-free
    on the asyncio event loop: there is no `await` between the done() check and
    create_task(), so no other coroutine can interleave."""
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (shouldn't happen from inside an async handler).
        return
    _refresh_task = loop.create_task(_refresh_cache())


@router.get("/containers")
async def get_container_stats(current_user: User = Depends(get_current_user)):
    """
    Get aggregate statistics across all running agent containers.

    Returns total CPU usage, memory consumption, and per-container breakdown.

    Non-blocking (#1096): served from a short-TTL cache that is refreshed by a
    background task. The request never waits on the Docker daemon, so a fleet
    of agents can no longer pin a uvicorn worker for ~6s and stall the UI.
    Adds three fields on top of the original contract: `cached` (bool),
    `stale` (bool), and `cache_age_seconds` (float|None).
    """
    if not docker_client:
        raise HTTPException(status_code=503, detail="Docker not available")

    now = time.monotonic()
    cached = _stats_cache["data"]
    age = now - _stats_cache["timestamp"] if cached is not None else None

    # Fresh cache hit — return instantly, no refresh needed.
    if cached is not None and age is not None and age < _CACHE_TTL:
        return {**cached, "cached": True, "stale": False, "cache_age_seconds": round(age, 1)}

    # Cache is stale or cold: kick off an out-of-band refresh and return now.
    _schedule_refresh()

    if cached is not None:
        # Serve stale data while the refresh runs (stale-while-revalidate).
        return {**cached, "cached": True, "stale": True, "cache_age_seconds": round(age, 1)}

    # Cold start (no data computed yet): return an instant, valid, empty
    # payload. The next poll (a few seconds later) gets real data once the
    # background refresh completes. No request ever pays the Docker latency.
    return {**_empty_payload(0), "cached": False, "stale": True, "cache_age_seconds": None}
