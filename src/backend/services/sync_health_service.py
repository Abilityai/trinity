"""
Sync Health Service (#389 S1).

Polls each git-enabled agent on an interval, reads its `/api/git/status`
response (which now carries dual ahead/behind + persisted sync-state from
the auto-sync heartbeat), upserts `agent_sync_state`, and emits a
`sync_failing` operator-queue entry when the consecutive-failures counter
crosses the alert threshold.

Lifecycle mirrors `OperatorQueueSyncService`:
  - `start()` kicks off `_poll_loop`
  - `stop()` cancels the task
  - Internally swallows exceptions so the heartbeat never dies silently

The poll interval defaults to 60 s — sync failures are slow-moving, and a
tighter loop would multiply SQLite writes for no benefit (PERF-269).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

from database import db
from services.agent_client import AgentClient
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 60  # seconds
ALERT_THRESHOLD = 3  # emit sync_failing entry after N consecutive failures

# WebSocket manager injected from main.py (optional, mirrors operator-queue pattern).
_websocket_manager = None


def set_websocket_manager(manager):
    global _websocket_manager
    _websocket_manager = manager


class SyncHealthService:
    """Background service that keeps agent_sync_state fresh and raises alerts."""

    def __init__(self, poll_interval: int = DEFAULT_POLL_INTERVAL):
        self.poll_interval = poll_interval
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # -------------------------- lifecycle --------------------------

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "Sync health service started (interval=%ss)", self.poll_interval
        )

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Sync health service stopped")

    # -------------------------- loop --------------------------

    async def _poll_loop(self):
        while self._running:
            try:
                await self._poll_cycle()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("sync health poll cycle raised")

            if self.poll_interval > 0:
                try:
                    await asyncio.sleep(self.poll_interval)
                except asyncio.CancelledError:
                    break
            else:
                # Test-only: single cycle, then exit.
                break

    async def _poll_cycle(self):
        """One pass over every git-enabled agent."""
        try:
            configs = db.list_git_enabled_agents()
        except Exception:
            logger.debug("could not list git-enabled agents", exc_info=True)
            return

        if not configs:
            return

        tasks = [self._sync_agent(cfg) for cfg in configs]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _sync_agent(self, config) -> None:
        """Pull one agent's git status, upsert DB, maybe alert."""
        agent_name = getattr(config, "agent_name", None) or config["agent_name"]

        payload = await self._fetch_git_status(agent_name)
        if payload is None:
            # Agent unreachable this tick — don't write anything; we'd rather
            # wait for a signal than flap the counter.
            return

        if not payload.get("git_enabled", True):
            # Git no longer enabled — nothing to track.
            return

        sync_state = payload.get("sync_state") or {}
        last_sync_status = sync_state.get("last_sync_status") or "never"
        last_sync_at = sync_state.get("last_sync_at")
        last_error_summary = sync_state.get("last_error_summary")

        last_commit = (payload.get("last_commit") or {})
        local_head_sha = last_commit.get("sha")

        prior = db.get_sync_state(agent_name)
        prior_failures = prior["consecutive_failures"] if prior else 0

        updated = db.upsert_sync_state(
            agent_name,
            last_sync_at=last_sync_at,
            last_sync_status=last_sync_status,
            last_error_summary=last_error_summary,
            last_remote_sha_main=payload.get("last_remote_sha_main"),
            last_remote_sha_working=local_head_sha,
            ahead_main=payload.get("ahead_main") or payload.get("ahead") or 0,
            behind_main=payload.get("behind_main") or payload.get("behind") or 0,
            ahead_working=payload.get("ahead_working") or 0,
            behind_working=payload.get("behind_working") or 0,
            last_check_at=utc_now_iso(),
        )

        # Edge-triggered alert: only emit when we cross the threshold.
        new_failures = updated["consecutive_failures"]
        if prior_failures < ALERT_THRESHOLD <= new_failures:
            self._emit_sync_failing_alert(agent_name, updated)

    async def _fetch_git_status(self, agent_name: str) -> Optional[Dict]:
        """Fetch /api/git/status from the agent. None on unreachable."""
        try:
            client = AgentClient(agent_name)
            response = await client.get("/api/git/status", timeout=10.0)
            if response.status_code != 200:
                return None
            return response.json()
        except Exception:
            logger.debug("agent %s unreachable", agent_name, exc_info=True)
            return None

    # -------------------------- alerting --------------------------

    def _emit_sync_failing_alert(self, agent_name: str, state: Dict) -> None:
        """Insert a sync_failing operator-queue entry.

        The ID embeds the emission timestamp so each distinct failure series
        produces a unique row. This method is only called on the edge where
        consecutive_failures crosses from (threshold-1) → threshold, so we
        fire at most once per series naturally.
        """
        now = utc_now_iso()
        last_sync_at = state.get("last_sync_at") or now
        item_id = f"sync-failing-{agent_name}-{now}"
        item = {
            "id": item_id,
            "agent_name": agent_name,
            "type": "sync_failing",
            "status": "pending",
            "priority": "high",
            "title": "Git sync failing",
            "question": (
                f"{agent_name}'s git sync has failed "
                f"{state['consecutive_failures']} times in a row."
            ),
            "context": {
                "last_error_summary": state.get("last_error_summary") or "",
                "last_sync_at": last_sync_at,
                "consecutive_failures": state["consecutive_failures"],
            },
            "created_at": now,
        }
        try:
            db.create_operator_queue_item(agent_name, item)
            logger.warning(
                "sync_failing emitted for %s (failures=%s)",
                agent_name, state["consecutive_failures"],
            )
        except Exception:
            logger.exception("failed to emit sync_failing alert")


# Module-level singleton mirrors operator_queue_service.
sync_health_service = SyncHealthService()
