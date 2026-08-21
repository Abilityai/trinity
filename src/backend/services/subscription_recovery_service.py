"""Background recovery probe for rate-limited subscriptions (#447).

## Why this loop exists

`rate_limited_now` clears in exactly one way: a **fresh provider verdict** that
says the quota is fine (`subscription_headroom_service.resolve_rate_limited_now`).
Nothing else can do it — `clear_rate_limit_events` has had no production caller
since #444, so the db predicate ("a failure row in the last 2 hours") only ever
decays with the clock, never with success.

Before this, the only thing that produced a fresh verdict was the **ambient
refresh**, which is *demand-driven*: it fires when somebody reads the dashboard
or the usage endpoint. So a subscription that came back to life while nobody was
looking stayed marked `LIMIT` until its failure rows aged out — and on an
unwatched instance, the provider was never asked again at all.

This loop asks. It probes only subscriptions that are currently **presented as
limited**, on a tighter cadence than the general refresh, without needing a
viewer.

## Why it is cheap, and why it cannot feed itself

The probe is the same `max_tokens=1` Haiku call the rest of #471 uses — a dozen
tokens of the operator's own quota. It runs only for the (normally empty) set of
limited subscriptions, and `_probe` records a 429 into the **snapshot only**,
never into `subscription_rate_limit_events` — so re-probing a still-limited
subscription can never manufacture the db failure row that keeps it limited.

## Gating

Reuses the existing, Settings-surfaced `subscription_headroom_auto_refresh`
toggle (default ON) rather than adding a second knob: that setting already
answers the only question that matters here — *may Trinity probe on its own?* —
and an operator who switched it off has said no to exactly this. Interval is
env-tunable (`SUBSCRIPTION_RECOVERY_PROBE_SECONDS`, default 300s).

Cross-worker leader-locked (the #1464 `monitoring:leader` shape) so `--workers 2`
does not double-probe. Fail-open on Redis for *leadership* — a duplicated probe
is a dozen wasted tokens, whereas failing closed would silently stop recovery
detection, the mode the operator cannot see — while the probe itself stays
fail-CLOSED on Redis inside `recover_probe`.
"""

import asyncio
import logging
import os
import uuid
from typing import Any, Dict, Optional

from database import db
from redis_breaker_util import get_breaker_redis
from services.subscription_headroom_service import (
    RECOVERY_PROBE_SECONDS,
    is_auto_refresh_enabled,
    recover_probe,
)

logger = logging.getLogger(__name__)

_LEADER_KEY = "subscription:recovery:leader"

# Idle poll while the toggle is off — short enough that flipping it back on
# takes effect without a backend restart, long enough to cost nothing.
_DISABLED_POLL_SECONDS = int(
    os.getenv("SUBSCRIPTION_RECOVERY_DISABLED_POLL_SECONDS", "300")
)

# An outcome worth an INFO line. Everything else is the steady state (no limited
# subscriptions at all), and logging that every 5 minutes forever is noise.
_NOTEWORTHY = ("recovered",)


class SubscriptionRecoveryService:
    """Probes subscriptions believed rate-limited until they come back (#447)."""

    def __init__(self) -> None:
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Unique per worker so the lease is only refreshed/released by its owner.
        self._worker_id = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._is_leader = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Subscription recovery probe service started")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        self._release_leadership()
        logger.info("Subscription recovery probe service stopped")

    # ------------------------------------------------------------------
    # Leadership (mirrors monitoring_service #1464 / skills_sync ent#236)
    # ------------------------------------------------------------------

    def _try_acquire_leadership(self, ttl: int) -> bool:
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
            logger.warning("subscription recovery leader check failed-open (%s)", e)
            return True

    def _release_leadership(self) -> None:
        try:
            r = get_breaker_redis()
            if r is not None and r.get(_LEADER_KEY) == self._worker_id:
                r.delete(_LEADER_KEY)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        while self._running:
            sleep_for = _DISABLED_POLL_SECONDS
            try:
                # Re-read the toggle EVERY cycle so flipping it applies without
                # a restart (the ent#236 rule).
                enabled = await asyncio.to_thread(is_auto_refresh_enabled)
                if enabled:
                    interval = max(60, RECOVERY_PROBE_SECONDS)
                    sleep_for = interval
                    leader = self._try_acquire_leadership(interval * 3)
                    if leader and not self._is_leader:
                        logger.info(
                            "Subscription recovery acquired leadership (worker %s)",
                            self._worker_id,
                        )
                    elif not leader and self._is_leader:
                        logger.info(
                            "Subscription recovery yielded leadership (worker %s)",
                            self._worker_id,
                        )
                    self._is_leader = leader
                    if leader:
                        await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a bad cycle must not kill the loop
                logger.error(f"Subscription recovery cycle failed: {e}")

            await asyncio.sleep(max(1, sleep_for))

    async def run_cycle(self) -> Dict[str, Any]:
        """One sweep over every subscription. Never raises."""
        try:
            subs = await asyncio.to_thread(db.list_subscriptions)
        except Exception as e:  # noqa: BLE001
            logger.warning("Subscription recovery could not list subscriptions: %s", e)
            return {"probed": 0, "error": str(type(e).__name__)}

        outcomes: Dict[str, int] = {}
        for sub in subs or []:
            sid = getattr(sub, "id", None)
            if not sid:
                continue
            try:
                outcome = await recover_probe(sid)
            except Exception as e:  # noqa: BLE001 — one bad subscription must not
                # end the sweep; its siblings are independent.
                logger.warning("Recovery probe failed for %s: %s", sid, e)
                outcome = "error"
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if outcome in _NOTEWORTHY:
                logger.info(
                    "Subscription %s is out of its rate limit — badge will clear", sid
                )

        return {"probed": sum(outcomes.values()), "outcomes": outcomes}


subscription_recovery_service = SubscriptionRecoveryService()
