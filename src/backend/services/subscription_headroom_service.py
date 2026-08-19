"""
Subscription headroom — provider-truth utilization snapshots (#471).

"How much is left until the limit" per subscription, sourced from the
``anthropic-ratelimit-unified-*`` response headers of a minimal probe call.
Verified 2026-08-19 against a real stored ``sk-ant-oat01-`` setup token:

- ``GET /api/oauth/usage`` is DEAD for setup tokens (403 ``permission_error:
  missing user:profile scope`` — that endpoint needs an interactive-login
  token; the mechanism behind closed PR #2170). Do not resurrect it here.
- ``POST /v1/messages`` under the same token returns the full unified header
  set: ``{5h,7d}-utilization`` (fraction 0..1), ``-reset`` (unix seconds),
  per-window ``-status``, ``representative-claim``, overage status/reason.

The probe is a real ``max_tokens=1`` Haiku message on the OPERATOR'S OWN
subscription token — it costs ~a dozen tokens of quota and appears in the
Anthropic console. Policy (operator-ruled at the #471 gate, post-critique):

- **Click-to-refresh** (``force=True``): always available, floored at
  ``MIN_PROBE_INTERVAL_SECONDS`` per subscription; works Redis-down via a
  per-worker in-process floor (the result is served uncached).
- **Ambient refresh** (``force=False``): default-ON behind the
  ``subscription_headroom_auto_refresh`` system setting; fires only when the
  cached snapshot is older than the refresh interval, single-flighted, and
  **fail-CLOSED when Redis is unavailable** — no cache ⇒ no ambient probe ⇒
  observed-only, so the 60s dashboard poll can never become a probe storm
  (the Gemini-critique #2 failure mode, impossible by construction).
- Probe 429s update the SNAPSHOT only — never ``subscription_rate_limit_events``
  (platform-caused telemetry, not agent work; polluting the stream would trip
  the badges the stream feeds).

``decorate_usage`` is the ONE place ``rate_limited_now`` gets its final
derivation (2h event predicate OR fresh provider verdict) — every surface
consumes it; no second definition (#2157 one-gate lesson).
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from database import db
from db_models import HeadroomWindow, SubscriptionHeadroom, SubscriptionUsage
from redis_breaker_util import get_breaker_redis, SingleFlightLock
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# Cheapest Claude model; the probe never reads the body, only the headers.
PROBE_MODEL = "claude-haiku-4-5-20251001"
PROBE_URL = "https://api.anthropic.com/v1/messages"
PROBE_TIMEOUT_SECONDS = 15.0

# Ambient refresh cadence (demand-driven — an unwatched instance probes nothing).
REFRESH_SECONDS = int(os.getenv("SUBSCRIPTION_HEADROOM_REFRESH_SECONDS", "900"))
# Floor between probes for one subscription, click OR ambient.
MIN_PROBE_INTERVAL_SECONDS = 60
# Past this age a snapshot no longer drives rate_limited_now (still shown, with age).
FRESHNESS_SECONDS = REFRESH_SECONDS * 2

AUTO_REFRESH_SETTING = "subscription_headroom_auto_refresh"

_SNAPSHOT_KEY = "subscription:headroom:{sid}"
_LOCK_KEY = "subscription:headroom_probe:{sid}"

# Per-worker fallback floor so a click can't spam probes while Redis is down.
_last_probe_at: Dict[str, float] = {}

_H = "anthropic-ratelimit-unified-"


def is_auto_refresh_enabled() -> bool:
    """Default ON (operator ruling) — system_settings row wins."""
    try:
        return db.get_setting_value(AUTO_REFRESH_SETTING, default="true") == "true"
    except Exception:
        return True


def _parse_utilization(raw: Optional[str]) -> Optional[float]:
    """Header carries a fraction (0.15 = 15%); tolerate an already-percent value."""
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v * 100.0, 1) if v <= 1.0 else round(v, 1)


def _parse_reset(raw: Optional[str]) -> Optional[str]:
    """Unix seconds → ISO-Z."""
    if raw is None:
        return None
    try:
        return (
            datetime.fromtimestamp(int(raw), tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def parse_unified_headers(headers) -> Optional[dict]:
    """Build a snapshot dict from a response's unified rate-limit headers.

    Returns None when the header family is entirely absent (endpoint drift or
    a non-subscription token) — the caller degrades to observed, loudly.
    Missing individual fields degrade to None, never raise.
    """
    def h(name: str) -> Optional[str]:
        return headers.get(_H + name)

    if not any(h(n) is not None for n in ("5h-utilization", "7d-utilization", "status")):
        return None

    def window(prefix: str) -> Optional[dict]:
        util = _parse_utilization(h(f"{prefix}-utilization"))
        reset = _parse_reset(h(f"{prefix}-reset"))
        status = h(f"{prefix}-status")
        if util is None and reset is None and status is None:
            return None
        return {"utilization_pct": util, "resets_at": reset, "status": status}

    return {
        "five_hour": window("5h"),
        "seven_day": window("7d"),
        "representative_claim": h("representative-claim"),
        "overage_status": h("overage-status"),
        "unified_status": h("status"),
    }


async def _probe(subscription_id: str) -> Optional[dict]:
    """One probe call; returns the snapshot dict (with status) or None when the
    subscription has no usable token. Never raises; never logs the token."""
    token = db.get_subscription_token(subscription_id)
    if not token:
        return None

    snapshot: Dict[str, Any] = {"fetched_at": utc_now_iso(), "status": "ok"}
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                PROBE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "anthropic-version": "2023-06-01",
                    "User-Agent": "claude-code/2.0.32",
                    "Content-Type": "application/json",
                },
                json={
                    "model": PROBE_MODEL,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
    except Exception as e:
        logger.warning(
            "[#471] headroom probe transport failure for subscription %s: %s",
            subscription_id, type(e).__name__,
        )
        return {"fetched_at": snapshot["fetched_at"], "status": "error"}

    # A 429 carries the same unified headers — a rate-limited subscription
    # reports its own true state. 401/403 = dead/invalid token.
    if resp.status_code in (401, 403):
        snapshot["status"] = "invalid_token"
    elif resp.status_code == 429:
        snapshot["status"] = "rate_limited"
    elif resp.status_code != 200:
        snapshot["status"] = "error"

    parsed = parse_unified_headers(resp.headers)
    if parsed is None:
        if snapshot["status"] == "ok":
            # 200 but no unified headers — endpoint drift; loud, not blank.
            logger.warning(
                "[#471] headroom probe for subscription %s returned HTTP %s "
                "with NO unified rate-limit headers — provider drift? "
                "Degrading to observed.",
                subscription_id, resp.status_code,
            )
            return {"fetched_at": snapshot["fetched_at"], "status": "error"}
        return snapshot
    snapshot.update(parsed)
    return snapshot


def _read_snapshot(subscription_id: str) -> tuple:
    """Returns ``(redis_ok, snapshot)``. The two falsy cases MUST stay
    distinct: "Redis answered: no snapshot" may ambient-probe; "Redis could
    not be asked" must NOT — a client OBJECT existing proves nothing about the
    server, and collapsing the two re-opens the probe-per-poll storm the
    fail-closed rule exists to prevent (the #2196 tri-state lesson applied to
    a cache)."""
    r = get_breaker_redis()
    if r is None:
        return False, None
    try:
        raw = r.get(_SNAPSHOT_KEY.format(sid=subscription_id))
        return True, (json.loads(raw) if raw else None)
    except Exception:
        return False, None


# Self-cleaning: a deleted subscription's snapshot dies with the TTL even when
# the best-effort DEL at delete time was missed (Redis down at that moment).
_SNAPSHOT_TTL_SECONDS = 7 * 24 * 3600


def _store_snapshot(subscription_id: str, snapshot: dict) -> None:
    r = get_breaker_redis()
    if r is None:
        return
    try:
        r.set(
            _SNAPSHOT_KEY.format(sid=subscription_id),
            json.dumps(snapshot),
            ex=_SNAPSHOT_TTL_SECONDS,
        )
    except Exception:
        pass


def clear_snapshot(subscription_id: str) -> None:
    """Best-effort cleanup on subscription delete."""
    r = get_breaker_redis()
    if r is None:
        return
    try:
        r.delete(_SNAPSHOT_KEY.format(sid=subscription_id))
    except Exception:
        pass


def _snapshot_age_seconds(snapshot: dict) -> Optional[int]:
    fetched = snapshot.get("fetched_at")
    if not fetched:
        return None
    try:
        dt = datetime.strptime(fetched, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return max(0, int(datetime.now(timezone.utc).timestamp() - dt.timestamp()))
    except (ValueError, TypeError):
        return None


def _probe_floor_ok(subscription_id: str, snapshot: Optional[dict]) -> bool:
    """MIN_PROBE_INTERVAL floor: snapshot age when we have one, else the
    per-worker in-process timestamp (the Redis-down click path)."""
    if snapshot is not None:
        age = _snapshot_age_seconds(snapshot)
        if age is not None and age < MIN_PROBE_INTERVAL_SECONDS:
            return False
    last = _last_probe_at.get(subscription_id)
    if last is not None and (time.monotonic() - last) < MIN_PROBE_INTERVAL_SECONDS:
        return False
    return True


def _to_model(snapshot: Optional[dict]) -> Optional[SubscriptionHeadroom]:
    if not snapshot:
        return None

    def win(d: Optional[dict]) -> Optional[HeadroomWindow]:
        return HeadroomWindow(**d) if d else None

    return SubscriptionHeadroom(
        five_hour=win(snapshot.get("five_hour")),
        seven_day=win(snapshot.get("seven_day")),
        representative_claim=snapshot.get("representative_claim"),
        overage_status=snapshot.get("overage_status"),
        fetched_at=snapshot.get("fetched_at"),
        snapshot_age_seconds=_snapshot_age_seconds(snapshot),
        status=snapshot.get("status", "ok"),
    )


# Strong refs for background refreshes — an asyncio.Task held only by the event
# loop can be GC'd mid-flight (the #1083 footgun the architecture documents).
_bg_refreshes: set = set()


async def _probe_and_store(subscription_id: str) -> Optional[dict]:
    _last_probe_at[subscription_id] = time.monotonic()
    fresh = await _probe(subscription_id)
    if fresh is not None:
        _store_snapshot(subscription_id, fresh)
    return fresh


async def _locked_probe(subscription_id: str) -> Optional[dict]:
    lock = SingleFlightLock(
        _LOCK_KEY.format(sid=subscription_id),
        ttl_seconds=int(PROBE_TIMEOUT_SECONDS) + 30,
        client=get_breaker_redis(),
    )
    if not lock.acquire():
        return None  # a sibling is probing — its result lands in the cache
    try:
        return await _probe_and_store(subscription_id)
    finally:
        lock.release_if_owned()


async def get_headroom(
    subscription_id: str, *, force: bool = False, wait: bool = True
) -> Optional[SubscriptionHeadroom]:
    """The one read path.

    ``force`` = operator click: probe now (floored at MIN_PROBE_INTERVAL;
    works Redis-down via the in-process floor, result served uncached).
    Ambient refresh (``force=False``) requires the toggle ON, a
    stale-or-absent cached snapshot, AND a Redis that ANSWERED the cache read
    (fail-closed — an unreachable Redis must not read as "no snapshot", see
    ``_read_snapshot``).

    ``wait=False`` (the dashboard batch path) runs an ambient refresh as a
    background task and serves the stale snapshot immediately — a hung
    provider must never wedge the 60s chassis poll behind N × probe-timeout.
    """
    redis_ok, snapshot = _read_snapshot(subscription_id)
    age = _snapshot_age_seconds(snapshot) if snapshot else None

    if force:
        if _probe_floor_ok(subscription_id, snapshot):
            fresh = await _locked_probe(subscription_id)
            if fresh is not None:
                snapshot = fresh
    elif (
        redis_ok
        and is_auto_refresh_enabled()
        and (snapshot is None or age is None or age > REFRESH_SECONDS)
        and _probe_floor_ok(subscription_id, snapshot)
    ):
        if wait:
            fresh = await _locked_probe(subscription_id)
            if fresh is not None:
                snapshot = fresh
        else:
            task = asyncio.create_task(_locked_probe(subscription_id))
            _bg_refreshes.add(task)
            task.add_done_callback(_bg_refreshes.discard)
            # Serve the stale/absent snapshot now; the refresh lands in the
            # cache for the next poll.

    return _to_model(snapshot)


def _headroom_indicates_limited(headroom: Optional[SubscriptionHeadroom]) -> bool:
    """Fresh provider verdict only — a stale snapshot never drives the badge."""
    if headroom is None:
        return False
    age = headroom.snapshot_age_seconds
    if age is None or age > FRESHNESS_SECONDS:
        return False
    if headroom.status == "rate_limited":
        return True
    for w in (headroom.five_hour, headroom.seven_day):
        if w and w.status and w.status not in ("allowed",):
            return True
    return False


async def decorate_usage(usage: SubscriptionUsage, *, force: bool = False) -> SubscriptionUsage:
    """Attach the provider snapshot + finalize `rate_limited_now` — the ONE
    derivation every surface consumes (#2157 one-gate lesson): the db layer's
    2h event predicate OR a fresh provider verdict. ``force`` = operator click
    (probe now, floored)."""
    headroom = await get_headroom(usage.subscription_id, force=force)
    if headroom is not None and headroom.status == "ok" and (
        headroom.five_hour or headroom.seven_day
    ):
        usage.source = "anthropic"
    usage.headroom = headroom
    usage.rate_limited_now = bool(
        usage.rate_limited_now or _headroom_indicates_limited(headroom)
    )
    return usage


async def pressure_states(
    subscription_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Per-subscription pressure for the fleet batch endpoint: 24h event count
    + the same one-gate `rate_limited_now` derivation + headroom summary."""
    counts_24h = db.get_failure_event_counts_by_subscription(hours=24)
    out: Dict[str, Dict[str, Any]] = {}
    for sid in subscription_ids:
        # wait=False: the dashboard's 60s poll is the DEMAND signal for a
        # refresh, never a caller that blocks on one.
        headroom = await get_headroom(sid, wait=False)
        limited = db.is_subscription_rate_limited(sid) or _headroom_indicates_limited(headroom)
        out[sid] = {
            "failure_events_24h": int(counts_24h.get(sid, 0)),
            "rate_limited_now": bool(limited),
            "headroom": headroom,
        }
    return out
