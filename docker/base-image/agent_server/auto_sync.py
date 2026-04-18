"""
Auto-sync heartbeat loop (#389 S1a).

Runs `_run_auto_sync_once()` on an interval so the fleet stays fresh even
when the operator never hits Sync. Counters persisted via the sync-state
file are the mechanism by which the backend's SyncHealthService raises a
red flag after N consecutive failures.

Gated by `GIT_SYNC_AUTO=true`. Interval from `GIT_SYNC_INTERVAL_SECONDS`
(default 900 = 15 min). The backend sets these env vars on agent creation
for GitHub-template agents; other agents opt in via the per-agent flag.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 900  # 15 min — spec §S1a
_HOME_DIR = Path("/home/developer")


def should_run_auto_sync() -> bool:
    """Return True when the environment says we should start the loop."""
    return os.getenv("GIT_SYNC_AUTO", "").lower() == "true"


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

    # Sleep first so containers that just started aren't penalized by a
    # heartbeat happening before .git is even initialized.
    await asyncio.sleep(interval)

    while True:
        try:
            if not (home / ".git").exists():
                logger.debug("auto-sync: no .git in %s, skipping cycle", home)
            else:
                result = _run_auto_sync_once(home)
                logger.info("auto-sync: %s", result.get("status"))
        except Exception:  # noqa: BLE001 — loop must never die
            logger.exception("auto-sync cycle raised unexpectedly")
        await asyncio.sleep(interval)


def schedule_auto_sync_if_enabled(app) -> None:
    """Attach an @app.on_event('startup') handler if the env flag is set."""
    if not should_run_auto_sync():
        logger.info("auto-sync disabled (set GIT_SYNC_AUTO=true to enable)")
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
