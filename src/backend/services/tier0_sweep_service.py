"""Tier-0 sweep loop (trinity-enterprise#935, checkpoint B).

The deterministic evaluator's single writer path (TD-1): every 60 s the Redis
leader grades up to 25 settled terminal runs through
`tier0_evaluator.sweep`. Started in every uvicorn worker — only the lease
holder ticks — and mirrors `skills_sync_service` (the #1464
`monitoring:leader` shape) exactly: SET NX EX to acquire, GET == self →
EXPIRE to refresh, release on stop.

**Fail-OPEN, by design (TD-8).** A Redis fault makes every worker a leader
for the tick; that is at worst a doubled read, because the write is
idempotent on the ent#366 partial unique index — two leaders grading one run
leave one row. Failing closed would instead silently stop grading in exactly
the mode the operator cannot see. A contended lease skips the tick; nothing
here waits on, retries toward, or sits in the path of a terminal execution
write (decision 16: zero edits to `task_execution_service`).

The write fence holds here too: this module takes no principal and imports
nothing from FastAPI or the auth layer; it reaches the writer only through
`tier0_evaluator`, whose entry point takes an execution id and nothing else.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from redis_breaker_util import get_breaker_redis
from services import eval_scorecard_service, tier0_evaluator

logger = logging.getLogger(__name__)

# One leader across all uvicorn workers — whoever holds it runs the tick.
_LEADER_KEY = "tier0:leader"

# TD-8: cadence; the lease outlives two missed refreshes (#1464 shape) so a
# dead holder's lease lapses within ~3 cycles and a sibling takes over.
SWEEP_INTERVAL_SECONDS = 60
LEASE_TTL_SECONDS = 3 * SWEEP_INTERVAL_SECONDS


class Tier0SweepService:
    """Background Tier-0 sweep loop (ent#935)."""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Unique per worker process so the lease is only ever refreshed or
        # released by its own holder.
        self._worker_id = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._is_leader = False
        # ent#937: the UTC day the nightly scorecard restate last ran in.
        self._nightly_day: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Tier-0 sweep service started")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        self._release_leadership()
        logger.info("Tier-0 sweep service stopped")

    # -------------------------------------------------------------------------
    # Leadership (mirrors skills_sync_service / monitoring_service #1464)
    # -------------------------------------------------------------------------

    def _try_acquire_leadership(self, ttl: int) -> bool:
        """True iff this worker owns the lease for this tick. Fail-open on a
        Redis error (see the module docstring for why that is safe here)."""
        r = get_breaker_redis()
        if r is None:
            return True
        try:
            if r.set(_LEADER_KEY, self._worker_id, nx=True, ex=ttl):
                return True
            if r.get(_LEADER_KEY) == self._worker_id:
                r.expire(_LEADER_KEY, ttl)
                return True
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning("tier0 sweep leader lock check failed-open (%s)", e)
            return True

    def _release_leadership(self) -> None:
        try:
            r = get_breaker_redis()
            if r is not None and r.get(_LEADER_KEY) == self._worker_id:
                r.delete(_LEADER_KEY)
        except Exception:  # noqa: BLE001
            pass

    # -------------------------------------------------------------------------
    # Loop
    # -------------------------------------------------------------------------

    async def _run_loop(self) -> None:
        while self._running:
            try:
                leader = self._try_acquire_leadership(LEASE_TTL_SECONDS)
                if leader and not self._is_leader:
                    logger.info("Tier-0 sweep acquired leadership (worker %s)", self._worker_id)
                elif not leader and self._is_leader:
                    logger.info("Tier-0 sweep yielded leadership (worker %s)", self._worker_id)
                self._is_leader = leader
                if leader:
                    await self.run_cycle()
                    await self.run_nightly_if_due()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a bad tick must not kill the loop
                logger.error("Tier-0 sweep tick failed: %s", e)
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)

    async def run_cycle(self) -> Dict[str, Any]:
        """One tick at the TD-8 bounds. `sweep` never raises."""
        result = await tier0_evaluator.sweep(
            limit=tier0_evaluator.SWEEP_LIMIT,
            settle_seconds=tier0_evaluator.SWEEP_SETTLE_SECONDS,
            lookback_hours=tier0_evaluator.SWEEP_LOOKBACK_HOURS,
            timeout_seconds=tier0_evaluator.EVALUATION_TIMEOUT_SECONDS,
            concurrency=tier0_evaluator.EVALUATION_CONCURRENCY,
        )
        if result.get("candidates"):
            logger.info("Tier-0 sweep: %s", result)
        return result

    async def run_nightly_if_due(self, now: Optional[datetime] = None) -> Optional[Dict[str, int]]:
        """ent#937: the scorecard restate, at the first leader tick after
        00:10 UTC, under the same lease. Idempotent, so a fail-open double
        leader or a restart re-running it costs a repeat write, not a wrong one."""
        now = now or datetime.now(timezone.utc)
        if not eval_scorecard_service.nightly_due(now, self._nightly_day):
            return None
        self._nightly_day = now.strftime("%Y-%m-%d")
        result = await asyncio.to_thread(eval_scorecard_service.nightly_pass, now)
        logger.info("Eval scorecard nightly: %s", result)
        return result


tier0_sweep_service = Tier0SweepService()
