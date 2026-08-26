"""Background subscription sweep: recovery probing (#447) + headroom sampling (ent#434).

The module keeps its `recovery` name because `main.py` and `test_447_*` bind
the symbol; the sweep now carries a second job.

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
does not double-probe. Leadership is fail-OPEN on Redis, and ent#434 re-prices
that choice, so the reasoning is restated rather than left as it was:

- Under #447 alone the probed set was normally EMPTY, so "a duplicated probe is
  a dozen wasted tokens" was true and failing closed would have silently stopped
  the only signal that clears a stale `LIMIT` badge.
- With the sampler the probed set is EVERY subscription, so the honest worst
  case is now `workers x N subscriptions` per cycle, not a dozen tokens.

It stays fail-open anyway, for a reason that survives the change: both halves
of the sweep are fail-CLOSED on Redis one level down (`recover_probe` and
`ensure_reading` each return early when `_read_snapshot` reports the server did
not answer), so a genuine Redis outage yields ZERO probes regardless of how
many workers believe they are the leader. The multiplier only applies when
Redis is reachable for reads and unreachable for the lease, which is a narrow
window — and failing the lease closed would stop recovery detection outright,
which is the failure the operator cannot see.

## ent#434 — headroom sampling

The sweep additionally refreshes each subscription's provider snapshot when it
is older than `SAMPLE_INTERVAL_SECONDS` and evaluates the weekly-window alert.
Sampling and evaluating are separate: evaluating is a cached read plus
arithmetic and happens every cycle, while probing is gated on its own elapsed
interval. That split is why the loop period stays at `RECOVERY_PROBE_SECONDS`
— raising it to "hourly" to match the sampler would raise the lease TTL with
it (`interval * 3`), leaving recovery detection dead for up to three hours
after a leader crash.
"""

import asyncio
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from database import db
from redis_breaker_util import get_breaker_redis
from services import subscription_headroom_alerts as alerts
from services.subscription_headroom_service import (
    RECOVERY_PROBE_SECONDS,
    SAMPLE_INTERVAL_SECONDS,
    classify_headroom,
    ensure_reading,
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


# ent#434: the sweep now touches EVERY subscription, not just the (normally
# empty) believed-limited set, so its wall-clock is no longer negligible.
# N subscriptions x a 15s probe timeout, run serially, can outlive the leader
# lease and let a sibling worker start probing concurrently — #1881's lesson
# one subsystem over. Bounded concurrency plus a between-chunk lease refresh
# keeps a cycle short and the lease continuously owned.
_MAX_CONCURRENT_PROBES = max(1, int(os.getenv("SUBSCRIPTION_SWEEP_CONCURRENCY", "4")))


def _chunks(items: List[Any], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


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

    async def _sweep_one(
        self, sub: Any, *, threshold: int, alerting: bool
    ) -> Dict[str, Any]:
        """Recovery, then headroom sampling, for ONE subscription.

        The two try/excepts are SIBLINGS, and that is load-bearing rather than
        tidiness: a bug in the ent#434 evaluation must never stop #447's
        recovery detection, which is the only mechanism that clears a stale
        `LIMIT` badge (nothing clears a failure row on success).

        Recovery runs FIRST. `_probe_floor_ok` bounds probes at 60s per
        subscription, so whichever consumer probes first floors the other out.
        Ordered this way a believed-limited subscription is refreshed by the
        path that wants the tight cadence, and `ensure_reading` then serves
        that zero-age snapshot without spending a second probe. Reversed,
        `recover_probe` would answer "floored" and #447 would quietly stop
        working — which is exactly the kind of failure nobody notices.
        """
        sid = sub.id
        result: Dict[str, Any] = {
            "sid": sid, "outcome": "error", "classification": None, "reading": None,
        }

        try:
            result["outcome"] = await recover_probe(sid)
        except Exception as e:  # noqa: BLE001 — one bad subscription must not
            # end the sweep; its siblings are independent.
            logger.warning("Recovery probe failed for %s: %s", sid, e)

        if not alerting:
            return result

        try:
            reading, _probed = await ensure_reading(
                sid, max_age_seconds=SAMPLE_INTERVAL_SECONDS
            )
            result["reading"] = reading
            result["classification"] = classify_headroom(
                reading,
                threshold_pct=threshold,
                max_age_seconds=alerts.MAX_READING_AGE_SECONDS,
            )
        except Exception as e:  # noqa: BLE001 — see above; the recovery outcome
            # recorded a moment ago still stands.
            logger.warning("Headroom sampling failed for %s: %s", sid, e)

        return result

    async def run_cycle(self) -> Dict[str, Any]:
        """One sweep over every subscription. Never raises.

        Two jobs share the sweep: #447 recovery probing and ent#434 headroom
        sampling + alert evaluation. They share it deliberately — a second loop
        would mean a second leader lease over the same probe budget, and the
        only thing between two leases and a double probe is a 60s per-worker
        floor, which is not a coordination primitive.
        """
        try:
            # `list_subscriptions`, deliberately NOT the with-agents variant:
            # that one issues a query per subscription, and the agent names are
            # needed only in the body of an alert that rarely fires. They are
            # fetched lazily in `_evaluate_alerts` instead, so a quiet fleet
            # pays one query per cycle rather than N+1.
            subs = await asyncio.to_thread(db.list_subscriptions)
        except Exception as e:  # noqa: BLE001
            logger.warning("Subscription sweep could not list subscriptions: %s", e)
            return {"probed": 0, "outcomes": {}, "error": str(type(e).__name__)}

        subs = [s for s in (subs or []) if getattr(s, "id", None)]
        if not subs:
            # Structurally important, not just an optimisation: `all([])` is
            # True, so a fleet-saturation verdict over an empty fleet would be
            # vacuously "every subscription is saturated" and alert an install
            # that has no subscriptions at all.
            return {"probed": 0, "outcomes": {}, "subscriptions": 0}

        try:
            threshold = await asyncio.to_thread(alerts.effective_threshold_pct)
        except Exception:  # noqa: BLE001
            threshold = alerts.DEFAULT_THRESHOLD_PCT
        alerting = threshold > 0

        results: List[Dict[str, Any]] = []
        lease_ttl = max(60, RECOVERY_PROBE_SECONDS) * 3
        for chunk in _chunks(subs, _MAX_CONCURRENT_PROBES):
            results.extend(await asyncio.gather(*[
                self._sweep_one(sub, threshold=threshold, alerting=alerting)
                for sub in chunk
            ]))
            # Re-assert the lease between chunks rather than only at the top of
            # the loop. Losing it mid-sweep means a sibling is already probing
            # the same fleet, so stop and let the partial results stand.
            if not self._try_acquire_leadership(lease_ttl):
                logger.info(
                    "Subscription sweep yielded the lease mid-cycle after %d/%d "
                    "subscriptions (worker %s)", len(results), len(subs), self._worker_id,
                )
                self._is_leader = False
                break

        outcomes: Dict[str, int] = {}
        for r in results:
            outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
            if r["outcome"] in _NOTEWORTHY:
                logger.info(
                    "Subscription %s is out of its rate limit — badge will clear",
                    r["sid"],
                )

        # Additional keys only — `probed`/`outcomes` are an asserted contract.
        summary: Dict[str, Any] = {
            "probed": sum(outcomes.values()),
            "outcomes": outcomes,
            "subscriptions": len(subs),
        }
        if alerting:
            summary.update(await self._evaluate_alerts(subs, results, threshold))
        else:
            # Same SHAPE as the alerting branches. It was a bare string, which
            # would have broken any consumer reading `summary["alerts"]["fleet"]`
            # the moment the threshold was set to 0.
            summary["alerts"] = {
                "fleet": False, "subscriptions": 0, "disabled": True,
            }
        return summary

    async def _evaluate_alerts(
        self, subs: List[Any], results: List[Dict[str, Any]], threshold: int
    ) -> Dict[str, Any]:
        """Decide and emit. Never raises — it runs after the recovery half has
        already done its job, and must not be able to undo it."""
        escalate_at = alerts.escalation_pct(threshold)
        by_sid = {s.id: s for s in subs}
        classifications = {
            r["sid"]: r["classification"] for r in results if r.get("classification")
        }
        verdict = alerts.fleet_verdict(classifications)

        # The fleet alert names every saturated subscription, so emitting the
        # individual ones beside it would be N+1 operator items for one event.
        if verdict["saturated"]:
            names = {s.id: (getattr(s, "name", None) or s.id) for s in subs}
            emitted = await asyncio.to_thread(
                alerts.emit_fleet_alert,
                verdict=verdict,
                names=names,
                threshold_pct=threshold,
                earliest_reset=_earliest_reset(results),
            )
            return {
                "alerts": {"fleet": bool(emitted), "subscriptions": 0},
                "fleet_blocked_reason": None,
            }

        emitted = 0
        capped = 0
        for r in results:
            if r.get("classification") != alerts.SATURATED:
                continue
            if emitted >= alerts.MAX_PER_SUBSCRIPTION_ALERTS_PER_CYCLE:
                capped += 1
                continue
            week = getattr(r.get("reading"), "seven_day", None)
            util = getattr(week, "utilization_pct", None)
            resets = getattr(week, "resets_at", None)
            tier = alerts.decide_tier(util, threshold, escalate_at)
            if tier is None:
                # Saturated with no comparable number — a probe 429, or a
                # window the provider reports as blocking with no figure. It
                # still counts toward the fleet claim (that is why it is
                # SATURATED), but a percentage-crossing alert cannot name a
                # percentage it does not have.
                continue
            sub = by_sid.get(r["sid"])
            try:
                agents = await asyncio.to_thread(
                    db.get_agents_by_subscription, r["sid"]
                )
            except Exception:  # noqa: BLE001 — the alert is worth more than
                # its agent list; degrade to naming none rather than not firing.
                agents = []
            ok = await asyncio.to_thread(
                alerts.emit_subscription_alert,
                subscription_id=r["sid"],
                subscription_name=getattr(sub, "name", None),
                tier=tier,
                utilization_pct=util,
                projected_end=alerts.project_end_utilization(util, resets),
                resets_at=resets,
                threshold_pct=threshold,
                agents=list(agents or []),
            )
            if ok:
                emitted += 1
        if capped:
            logger.warning(
                "[headroom-alert] %d further subscriptions crossed the threshold "
                "this cycle and were not alerted (per-cycle cap %d)",
                capped, alerts.MAX_PER_SUBSCRIPTION_ALERTS_PER_CYCLE,
            )
        return {
            "alerts": {"fleet": False, "subscriptions": emitted, "capped": capped},
            "fleet_blocked_reason": verdict["blocked_reason"],
        }


def _earliest_reset(results: List[Dict[str, Any]]) -> Optional[str]:
    """The first wall the fleet hits — used as the fleet episode key so the
    alert re-arms once per window rather than once per day."""
    resets = []
    for r in results:
        week = getattr(r.get("reading"), "seven_day", None)
        value = getattr(week, "resets_at", None)
        if value:
            resets.append(str(value))
    return min(resets) if resets else None


subscription_recovery_service = SubscriptionRecoveryService()
