"""
Auto-sync heartbeat loop (#389 S1a).

Runs `_run_auto_sync_once()` on an interval so the fleet stays fresh even
when the operator never hits Sync. Counters persisted via the sync-state
file are the mechanism by which the backend's SyncHealthService raises a
red flag after N consecutive failures.

Gated per CYCLE on the owner's `auto_sync_enabled` flag (#3010), read from
the platform with the agent's own MCP key (`GET /api/agents/{name}/git/
auto-sync`), so `PUT .../git/auto-sync` takes effect on the next cycle with no
recreate. `GIT_SYNC_AUTO` — derived from that same DB flag at every recreate —
is only the fallback when the platform cannot be asked. Interval from
`GIT_SYNC_INTERVAL_SECONDS` (default 900 = 15 min).
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 900  # 15 min — spec §S1a
_HOME_DIR = Path("/home/developer")


_FLAG_TIMEOUT = 10  # seconds — one small read per 15-min cycle

# The value the last cycle ran with (#3010). None until the first cycle asks;
# `/api/git/status` reports `current_auto_sync_enabled()`.
_last_resolved: Optional[bool] = None


def should_run_auto_sync() -> bool:
    """The env fallback: True when the container was baked with auto-sync on.

    Since #3010 the env is derived from the DB flag at every recreate, so this
    is the last value the platform handed the container — used only when the
    platform cannot be asked.
    """
    return os.getenv("GIT_SYNC_AUTO", "").lower() == "true"


def _platform_coords() -> Optional[tuple]:
    backend_url = os.getenv("TRINITY_BACKEND_URL")
    mcp_key = os.getenv("TRINITY_MCP_API_KEY")
    agent_name = os.getenv("AGENT_NAME")
    if backend_url and mcp_key and agent_name:
        return backend_url.rstrip("/"), mcp_key, agent_name
    return None


def should_start_loop() -> bool:
    """Start the loop when auto-sync is on OR could be turned on live: an agent
    that can ask the platform picks up an ON toggle without a recreate."""
    return should_run_auto_sync() or _platform_coords() is not None


async def resolve_auto_sync_enabled(client: httpx.AsyncClient) -> bool:
    """This cycle's gate: the owner's `auto_sync_enabled`, asked live (#3010).

    - 200 → the flag.
    - 404 "Git not configured" → False: nothing is bound to sync to.
    - anything else (backend down, 5xx, auth refusal, a uniform 404) → the env
      fallback, which the last recreate derived from the same flag. Failing to
      the last known value keeps a platform blip from arming a disabled loop or
      silencing an enabled one.
    """
    global _last_resolved
    value = should_run_auto_sync()
    coords = _platform_coords()
    if coords is not None:
        backend_url, mcp_key, agent_name = coords
        try:
            resp = await client.get(
                f"{backend_url}/api/agents/{agent_name}/git/auto-sync",
                headers={"Authorization": f"Bearer {mcp_key}"},
                timeout=_FLAG_TIMEOUT,
            )
            if resp.status_code == 200:
                value = bool(resp.json().get("auto_sync_enabled"))
            elif resp.status_code == 404 and _detail(resp) == "Git not configured":
                value = False
            else:
                logger.warning(
                    "auto-sync: flag read returned %s; using env fallback (%s)",
                    resp.status_code, value,
                )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("auto-sync: flag read failed (%s); using env fallback (%s)",
                           type(exc).__name__, value)
    _last_resolved = value
    return value


def _detail(resp: httpx.Response) -> Optional[str]:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("detail") if isinstance(body, dict) else None


def current_auto_sync_enabled() -> bool:
    """The value the loop is running with: the last cycle's resolution, or the
    env fallback before the first cycle has asked."""
    return _last_resolved if _last_resolved is not None else should_run_auto_sync()


def get_interval_seconds() -> int:
    raw = os.getenv("GIT_SYNC_INTERVAL_SECONDS")
    if not raw:
        return _DEFAULT_INTERVAL
    try:
        value = int(raw)
        return value if value > 0 else _DEFAULT_INTERVAL
    except ValueError:
        logger.warning("GIT_SYNC_INTERVAL_SECONDS=%r is not an int; using default", raw)
        return _DEFAULT_INTERVAL


async def run_auto_sync_loop(
    home_dir: Optional[Path] = None, interval_seconds: Optional[int] = None
) -> None:
    """Background loop. Swallows every exception to keep heartbeating."""
    from .routers.git import _run_auto_sync_once  # lazy: avoids circular import

    home = home_dir or _HOME_DIR
    interval = interval_seconds if interval_seconds is not None else get_interval_seconds()
    logger.info("auto-sync loop started (interval=%ss, home=%s)", interval, home)

    async with httpx.AsyncClient() as client:
        # Read the flag once up front so `/api/git/status` reports the owner's
        # value from boot, not the env fallback for the first interval (#3010).
        try:
            await resolve_auto_sync_enabled(client)
        except Exception:  # noqa: BLE001 — loop must never die
            logger.exception("auto-sync: initial flag read raised unexpectedly")

        # Sleep first so containers that just started aren't penalized by a
        # heartbeat happening before .git is even initialized.
        await asyncio.sleep(interval)

        while True:
            try:
                await run_one_cycle(client, home, _run_auto_sync_once)
            except Exception:  # noqa: BLE001 — loop must never die
                logger.exception("auto-sync cycle raised unexpectedly")
            await asyncio.sleep(interval)


async def run_one_cycle(client: httpx.AsyncClient, home: Path, run_once) -> Optional[dict]:
    """One tick: skip without a repo, skip when the owner has auto-sync off,
    else run the sync cycle. Returns the cycle result, or None when skipped."""
    if not (home / ".git").exists():
        logger.debug("auto-sync: no .git in %s, skipping cycle", home)
        return None
    if not await resolve_auto_sync_enabled(client):
        logger.info("auto-sync: off (auto_sync_enabled=0), skipping cycle")
        return None
    # #1595: run the cycle in a worker thread. A blocking cycle (repack can
    # run for many minutes) starves the event loop — /health unreachable, 5s
    # heartbeats missed, chat dead. The cycle's subprocesses are
    # sweep-registered and the repo lock serializes it against operator git
    # endpoints.
    result = await asyncio.to_thread(run_once, home)
    logger.info("auto-sync: %s", result.get("status"))
    return result


def schedule_auto_sync_if_enabled(app) -> None:
    """Attach the loop's startup handler when auto-sync is on or can be turned
    on live (#3010); each cycle then checks the owner's flag."""
    if not should_start_loop():
        logger.info(
            "auto-sync unavailable (no GIT_SYNC_AUTO and no platform "
            "credentials to read the per-agent flag)"
        )
        return

    task_ref: list[asyncio.Task] = []

    @app.on_event("startup")
    async def _start_auto_sync() -> None:
        task = asyncio.create_task(run_auto_sync_loop())
        task_ref.append(task)

    @app.on_event("shutdown")
    async def _stop_auto_sync() -> None:
        for task in task_ref:
            task.cancel()
