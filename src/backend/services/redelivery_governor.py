"""Correlated-failure / shared-cause re-delivery governor (#1085).

A leaf service that detects a **fleet-wide** cause behind correlated terminal
failures and pauses re-delivery for the whole fleet while it persists. It guards
the live #1083 fire-and-forget callback path (and, transport-agnostically, the
future pull-mode ``/api/internal/tasks/{id}/result`` path) against a thundering
herd: after a backend restart ~N agents re-send persisted terminal envelopes and
in-flight retries at once; if they are all failing for one reason (Claude API
outage, an expired platform key, a bad skill pushed fleet-wide) we want to stop
hammering rather than amplify the storm.

Design:
  * **Count distinct agents, not events.** A fleet cause is *many different*
    agents failing — a single crash-looping agent must not arm the pause. State
    is a Redis ZSET (member=agent_name, score=now); repeat failures from the
    same agent just refresh its score, so ``ZCARD`` is the distinct-agent count
    in the rolling window.
  * **Auto-expiring pause, no explicit unpause.** Arming sets a TTL'd flag; the
    TTL lapse is the recovery, so there is no stuck-pause failure mode. A storm
    that persists re-arms (refreshing the TTL).
  * **Fail-open everywhere.** Redis unreachable → never pause, never block. The
    underlying failure surfaces on its own; the governor only ever *reduces*
    pressure, never adds a new way to drop a terminal.

The gating flag (``REDELIVERY_GOVERNOR_ENABLED``) is checked by *callers* at
each read point, not here, so this module stays a pure mechanism. All Redis
plumbing is shared with the circuit breakers via ``redis_breaker_util`` (one
fail-open client, one reset path).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from redis_breaker_util import get_breaker_redis, reset_breaker_redis_client

logger = logging.getLogger(__name__)

# Redis keys (agent:* / governor:* convention — within the backend ACL).
_CORR_KEY = "governor:corr_failures"   # ZSET member=agent_name score=epoch_seconds
_PAUSE_KEY = "governor:pause"          # STRING "1", TTL'd auto-expiry

# Error codes that signal a *shared* cause when many agents report them at once.
# AUTH = no/invalid key or subscription; BILLING = rate-limit / credit / billing.
# Both are fleet-correlated when a platform key expires or the Claude API throws
# fleet-wide 429s; a TIMEOUT/AGENT_ERROR is per-task and intentionally excluded.
_CORRELATED_CODES = frozenset({"auth", "billing"})


class RedeliveryGovernor:
    """Distinct-agent correlated-failure detector + fleet pause flag.

    Stateless beyond Redis — safe to share one process-wide singleton across
    workers (each reads/writes the same shared Redis keys).
    """

    def record_terminal_failure(self, agent_name: str, error_code: Optional[str]) -> None:
        """Record an AUTH/BILLING terminal for ``agent_name`` and arm the
        fleet pause when distinct failing agents cross the threshold.

        Fail-open: a Redis outage records nothing and arms nothing. MUST be
        called only on the CAS-won branch (the caller's responsibility) so a
        replayed/late callback never double-counts.
        """
        if not error_code or str(error_code).lower() not in _CORRELATED_CODES:
            return
        r = get_breaker_redis()
        if r is None:
            return  # fail-open: no Redis → no detection

        import config

        window = config.CORRELATED_FAILURE_WINDOW_SECONDS
        threshold = config.CORRELATED_FAILURE_THRESHOLD
        pause_ttl = config.CORRELATED_PAUSE_TTL_SECONDS

        now = time.time()
        cutoff = now - window
        try:
            pipe = r.pipeline()
            pipe.zremrangebyscore(_CORR_KEY, 0, cutoff)   # drop stale agents
            pipe.zadd(_CORR_KEY, {agent_name: now})       # repeat = same member, refreshed score
            pipe.zcard(_CORR_KEY)                          # distinct agents in window
            pipe.expire(_CORR_KEY, window + 1)
            results = pipe.execute()
            distinct = int(results[2])
        except Exception as e:  # noqa: BLE001 — fail-open
            logger.warning("[#1085] governor record failed-open (%s)", e)
            reset_breaker_redis_client()
            return

        if distinct >= threshold:
            # Re-arming while the storm persists refreshes the TTL — recovery is
            # the TTL lapse once distinct failures stop crossing the threshold.
            try:
                r.set(_PAUSE_KEY, "1", ex=pause_ttl)
                logger.warning(
                    "[#1085] correlated-failure pause ARMED — %d distinct agents "
                    "failed AUTH/BILLING in %ds (threshold %d); re-delivery paused %ds",
                    distinct, window, threshold, pause_ttl,
                )
            except Exception as e:  # noqa: BLE001 — fail-open
                logger.warning("[#1085] governor pause-set failed-open (%s)", e)
                reset_breaker_redis_client()

    def is_paused(self) -> bool:
        """True while the fleet re-delivery pause is armed. Fail-open: Redis
        unreachable → False (never pause on a blip)."""
        r = get_breaker_redis()
        if r is None:
            return False
        try:
            return bool(r.get(_PAUSE_KEY))
        except Exception as e:  # noqa: BLE001 — fail-open
            logger.warning("[#1085] governor is_paused failed-open (%s)", e)
            reset_breaker_redis_client()
            return False

    def should_hold_reaper(self) -> bool:
        """Whether the lease reaper / capacity drain should hold off destructive
        re-delivery work. Identical to ``is_paused`` today; a named alias so the
        read points read intent-fully and a future policy split is local."""
        return self.is_paused()

    def distinct_failing_count(self) -> int:
        """Current distinct-agent count in the rolling window (observability /
        tests). Fail-open: Redis unreachable → 0."""
        r = get_breaker_redis()
        if r is None:
            return 0
        try:
            import config

            now = time.time()
            cutoff = now - config.CORRELATED_FAILURE_WINDOW_SECONDS
            r.zremrangebyscore(_CORR_KEY, 0, cutoff)
            return int(r.zcard(_CORR_KEY))
        except Exception as e:  # noqa: BLE001 — fail-open
            logger.warning("[#1085] governor count failed-open (%s)", e)
            reset_breaker_redis_client()
            return 0


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_governor: Optional[RedeliveryGovernor] = None


def get_redelivery_governor() -> RedeliveryGovernor:
    """Return the process-wide governor singleton."""
    global _governor
    if _governor is None:
        _governor = RedeliveryGovernor()
    return _governor
