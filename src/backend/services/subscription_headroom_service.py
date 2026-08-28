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
import math
import os
import time
from dataclasses import dataclass
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
# #447: how often a subscription BELIEVED LIMITED is re-probed to see whether it
# is back. Tighter than REFRESH_SECONDS on purpose — a healthy subscription's
# utilization drifting for 15 minutes costs nothing, whereas a recovered one
# still displaying `LIMIT` is actively wrong, and this is the only signal that
# can clear it (nothing clears a failure row on success, see
# `resolve_rate_limited_now`).
RECOVERY_PROBE_SECONDS = int(os.getenv("SUBSCRIPTION_RECOVERY_PROBE_SECONDS", "300"))

# ent#434: how often the background sampler refreshes a subscription that
# nobody is watching. Deliberately a FLOORED CONSTANT, not an operator knob
# (#1644): the operator cannot reason about the right value, and a mutable
# constant read at action time gating provider spend is the shape #1638 warns
# about. Hourly is ample for a window that spans a week, and it is 4x LESS
# than a watched instance already spends via REFRESH_SECONDS.
# Not env-readable, deliberately. An earlier revision read
# `SUBSCRIPTION_SAMPLE_INTERVAL_SECONDS` here, which contradicted the sentence
# above AND could never be set: neither compose uses `env_file`, so a var that
# is not an explicit `environment:` entry never reaches the container. An env
# read that cannot be satisfied is the worst of both — it reads as configurable,
# is inert, and invites a later "packaging fix" that creates exactly the knob
# this comment argues against.
SAMPLE_INTERVAL_SECONDS = max(REFRESH_SECONDS, 3600)

# ent#434 / #2409 — how old a reading may be and still be CLASSIFIED or RANKED.
# Deliberately longer than the display predicates' FRESHNESS_SECONDS: a 7-day
# window does not move meaningfully in a couple of hours, whereas a badge
# showing a stale number is wrong immediately. Two consumers, ONE constant
# (`subscription_headroom_alerts` re-exports it):
#   - the weekly alert's fleet arm needs every subscription simultaneously
#     assessable, which decays exponentially with fleet size (at a 2% per-probe
#     failure rate and N=50, all-fresh is a 36% event), so a tight bound would
#     make that alert unreachable on exactly the large fleet it matters for;
#   - the auto-switch ranker reads whatever the sampler left behind, and any
#     bound under the sampler cadence reads most candidates as UNKNOWN on an
#     unwatched instance — the feature would ship inert. Hence the `max`: the
#     bound always covers two sampler intervals (pinned by test).
MAX_READING_AGE_SECONDS = max(2 * 3600, 2 * SAMPLE_INTERVAL_SECONDS)

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
        # fromisoformat, not a fixed strptime format: utc_now_iso() emits
        # microseconds, and a seconds-only format made every age None — which
        # read as "refresh due" on every poll (only the 60s in-process floor
        # held) AND as "never fresh" for the limited-verdict/gauge. Caught
        # live on the first deployed probe.
        dt = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
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


def _history_row(snapshot: dict) -> dict:
    """Shape a probe snapshot for persistence, classifying the one ambiguous case.

    `parse_unified_headers` returns a non-None result when the bare top-level
    `status` header is present even if NEITHER window is, so `status='ok'` with
    both windows None is reachable. Persisting that verbatim yields a row whose
    every utilization column is NULL under an "ok" status — indistinguishable
    from a botched write, and permanently so once it is in the table. Recording
    it as `no_windows` makes the row explain itself.

    Deliberately history-LOCAL: the live #471 snapshot keeps its own status
    vocabulary untouched. Both readers of that field already treat this case
    correctly by inspecting the windows (`decorate_usage` requires a truthy
    window before claiming `source='anthropic'`; `_headroom_indicates_limited`
    iterates them), so there is nothing to fix upstream and no reason to perturb
    a just-merged surface.
    """
    row = dict(snapshot)
    if row.get("status") == "ok" and not row.get("five_hour") and not row.get("seven_day"):
        row["status"] = "no_windows"
    return row


async def _record_history(subscription_id: str, snapshot: dict) -> None:
    """Persist one probe result as a durable row (ent#433). Enrichment only.

    Two properties are load-bearing and neither is optional:

    1. **Off the event loop.** `db.*` is synchronous SQLAlchemy, the platform DB
       is DELETE-journal with a 30s busy timeout, and this coroutine runs inside
       a fire-and-forget task on the dashboard-poll path. A sync write landing
       during the 03:30 backup (which holds a read transaction for its whole
       duration) or the 04:30 VACUUM (exclusive, minutes) would block the ENTIRE
       loop — /health, the WS dispatcher, every in-flight request — for up to
       30s. `try/except` handles errors, not blocking; `to_thread` handles both.
    2. **`except Exception`, never `BaseException`.** Backend shutdown cancels
       this await, and `CancelledError` is not an `Exception` — it must keep
       propagating so the loop can actually stop.

    Never raises: the probe's own result and the Redis snapshot are already
    committed by the time this runs, and history is worth strictly less than
    either (the issue's one hard constraint).
    """
    try:
        await asyncio.to_thread(
            db.insert_headroom_history, subscription_id, _history_row(snapshot)
        )
    except Exception as e:  # noqa: BLE001 — enrichment must never break the probe
        logger.warning(
            "[ent#433] headroom history write failed for subscription %s: %s",
            subscription_id, type(e).__name__,
        )


async def _probe_and_store(subscription_id: str) -> Optional[dict]:
    _last_probe_at[subscription_id] = time.monotonic()
    fresh = await _probe(subscription_id)
    if fresh is not None:
        # ORDER IS LOAD-BEARING (pinned by test): the Redis snapshot is what
        # every live surface reads, so it is written FIRST and can never be
        # delayed by a slow or contended history write. ent#433.
        _store_snapshot(subscription_id, fresh)
        await _record_history(subscription_id, fresh)
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


def _believed_limited(subscription_id: str, snapshot: Optional[dict]) -> bool:
    """Is this subscription currently PRESENTED as rate-limited? (#447)

    Deliberately the union of both arms the display resolver can fire on, so
    the recovery probe targets exactly the set of subscriptions wearing a
    `LIMIT` badge — including the common case where the badge comes from the
    stale 2h db predicate and no provider snapshot disagrees with it yet.
    """
    if snapshot and snapshot.get("status") == "rate_limited":
        return True
    try:
        return bool(db.is_subscription_rate_limited(subscription_id))
    except Exception:  # noqa: BLE001 — a db blip must not stall the sweep
        return False


async def recover_probe(subscription_id: str) -> str:
    """Re-probe ONE subscription believed limited; return a short outcome (#447).

    Never raises. Fail-CLOSED on Redis, exactly like the ambient path: a client
    object existing proves nothing about the server, and without a readable
    cache the probe's result could not be stored for anyone to see — so it
    would be quota spent for nothing.

    Safe to run repeatedly against a still-limited subscription: ``_probe``
    records a 429 into the SNAPSHOT only and never writes
    ``subscription_rate_limit_events``, so this loop can never feed the very db
    predicate it exists to clear.
    """
    redis_ok, snapshot = _read_snapshot(subscription_id)
    if not redis_ok:
        return "redis_unavailable"
    if not _believed_limited(subscription_id, snapshot):
        return "not_limited"
    age = _snapshot_age_seconds(snapshot) if snapshot else None
    if age is not None and age < RECOVERY_PROBE_SECONDS:
        return "too_soon"
    if not _probe_floor_ok(subscription_id, snapshot):
        return "floored"
    fresh = await _locked_probe(subscription_id)
    if fresh is None:
        return "no_token"
    status = fresh.get("status")
    return "recovered" if status == "ok" else f"still_{status}"


# THE provider window-status vocabulary (#2396). One constant, two predicates —
# they disagreed for exactly one value and that was the bug.
#
# This is an ALLOWLIST of statuses observed to mean "requests are being served",
# never a blocklist of statuses that mean "blocked". The direction is deliberate
# and load-bearing: the provider does not publish this vocabulary, so an
# unrecognised status MUST read as blocking. (The inverse mistake — a deny-check
# that silently admits every future value — is the #848 lesson in
# docs/memory/learnings.md; here the safe default runs the other way.)
#
# `allowed_warning` is the provider's own near-the-limit tier and was absent
# from this set, so every subscription approaching its weekly window was
# reported rate-limited while the provider was still serving it. Verified on a
# live instance: a snapshot recording `seven_day: 90%, allowed_warning` sat
# beside 47 successful executions, zero `subscription_rate_limit_events`, and
# `overage_status: allowed` — the provider said allowed, and meant it.
NON_BLOCKING_WINDOW_STATUSES = frozenset({"allowed", "allowed_warning"})


def _headroom_indicates_limited(headroom: Optional[SubscriptionHeadroom]) -> bool:
    """Fresh provider verdict only — a stale snapshot never drives the badge.

    Window statuses are judged against ``NON_BLOCKING_WINDOW_STATUSES``, so an
    unrecognised status still reads as limited. Note the ordering: an actual
    HTTP 429 sets the top-level ``status`` and is caught FIRST, above any window
    status — so the window arm is the weaker, secondary signal, and the db
    predicate in ``resolve_rate_limited_now`` is a third, independent one.
    """
    if headroom is None:
        return False
    age = headroom.snapshot_age_seconds
    if age is None or age > FRESHNESS_SECONDS:
        return False
    if headroom.status == "rate_limited":
        return True
    for w in (headroom.five_hour, headroom.seven_day):
        if w and w.status and w.status not in NON_BLOCKING_WINDOW_STATUSES:
            return True
    return False


def _headroom_indicates_healthy(headroom: Optional[SubscriptionHeadroom]) -> bool:
    """A FRESH probe that actually reached the quota and found headroom (#447).

    The mirror of ``_headroom_indicates_limited``, and deliberately **not** its
    negation: "not limited" is also true for a stale snapshot, a rejected token
    and a transport error, none of which are evidence the subscription is
    usable. Only a fresh ``ok`` snapshot carrying at least one window, every
    reported window non-blocking, is positive proof of headroom.

    **``allowed_warning`` counts as headroom (#2396), and that is a decision,
    not an accident.** It is the provider's near-the-limit tier: the quota was
    reached, the answer was "yes", and the request was served. The states this
    predicate deliberately excludes are stale / rejected-token / transport
    error — "none of which are evidence the subscription is usable" (#447) — and
    a warning-tier reading plainly IS such evidence. Excluding it would keep a
    stale ``LIMIT`` badge on a working subscription for up to the db predicate's
    two hours, which is the #447 bug returning in a narrower window.

    It is **not** an assessability gate, and an earlier revision of this
    docstring wrongly said ent#434 would consume it as one. It returns
    ``False`` for a stale snapshot, a rejected token, a transport error **and**
    a genuinely saturated subscription — so it cannot tell "no evidence" from
    "no headroom", and the most saturated subscription in a fleet is
    indistinguishable from an unreachable one. The weekly-window alert path
    needs exactly that distinction and uses ``classify_headroom`` below.

    The BODY is deliberately unchanged: ``resolve_rate_limited_now`` consumes
    this predicate, so a behavioural edit here moves every ``LIMIT`` badge in
    the product (learnings 2026-08-25).
    """
    if headroom is None:
        return False
    age = headroom.snapshot_age_seconds
    if age is None or age > FRESHNESS_SECONDS:
        return False
    if headroom.status != "ok":
        return False
    windows = [w for w in (headroom.five_hour, headroom.seven_day) if w is not None]
    if not windows:
        return False
    return all(
        w.status is None or w.status in NON_BLOCKING_WINDOW_STATUSES for w in windows
    )


# ent#434 — the weekly-window verdict, three-state on purpose.
#
# `_headroom_indicates_limited` / `_headroom_indicates_healthy` above are a
# BINARY pair over "is this subscription usable right now", and both collapse
# every not-usable reason into one `False`. The alert path needs the third
# state: a subscription can be *known saturated*, *known to have room*, or
# *not assessable at all*, and conflating the last two is how a positive
# fleet-wide claim gets made on absent evidence (the ent#100 rule).
SATURATED = "saturated"
HAS_HEADROOM = "has_headroom"
UNASSESSABLE = "unassessable"


def classify_headroom(
    headroom: Optional[SubscriptionHeadroom],
    *,
    threshold_pct: float,
    max_age_seconds: Optional[int] = None,
) -> str:
    """Classify one subscription's WEEKLY window as saturated / has-headroom /
    unassessable (ent#434).

    Keyed on **utilization**, not on window status — those axes are orthogonal
    and conflating them reproduces #2396 in a new place. `allowed_warning` is
    deliberately non-blocking (the provider is serving the request), so a
    status-driven classifier would file a live 90% reading as `has_headroom`,
    which is the exact reading this feature exists to alarm on.

    Window status still carries information, just a *stronger* one: a 7d window
    the provider reports as blocking is past warning, and a probe that came
    back 429 is the provider refusing outright. Both are `saturated` regardless
    of whether a number came with them.

    Every "we could not tell" path returns `unassessable`, never
    `has_headroom` — including the easily-missed one where the provider
    reports a 5h window and no 7d window at all (`parse_unified_headers`
    admits that shape: its top guard passes on `5h-utilization` alone). A gate
    written as "ok status + non-blocking windows" would claim weekly headroom
    on zero weekly evidence.

    `utilization_pct` is `Optional` INDEPENDENTLY of status, so it is never
    coerced: a missing number cannot be compared to a threshold, and coercing
    it to 0 or 100 invents a reading in the direction of the coercion.

    Since #2409 this is POLICY over `headroom_reading` — the one usability
    gate the auto-switch ranker shares, so the two cannot drift. The verdicts
    are byte-identical to the pre-#2409 function; a differential test against
    a frozen copy pins every (age × status × window × threshold) pair.
    """
    reading = headroom_reading(headroom, max_age_seconds=max_age_seconds)
    if reading is None:
        return UNASSESSABLE

    # The provider refused the probe outright — the strongest saturation
    # evidence there is, and it arrives with no utilization figure.
    if reading.provider_status == "rate_limited":
        return SATURATED
    # invalid_token: the gate let it through because the probe DID answer,
    # but we learned nothing about the quota. Never a headroom claim.
    if reading.provider_status != "ok":
        return UNASSESSABLE

    week = reading.seven_day
    if week is None:
        return UNASSESSABLE
    if week.blocked:
        return SATURATED
    if week.utilization_pct is None:
        return UNASSESSABLE
    return SATURATED if week.utilization_pct >= threshold_pct else HAS_HEADROOM


# =============================================================================
# #2409 — the shared reading gate + the auto-switch ranker
# =============================================================================
#
# `classify_headroom` above and the selector below both need the same first
# question answered — "is this snapshot usable at all?" — and #2409's own
# warning is that two answers drift. So the gate is ONE function,
# `headroom_reading`, and each consumer is a few lines of POLICY over it: the
# classifier keys on the alert threshold; the ranker deliberately never sees
# a threshold (an operator's alert knob must not steer where the fleet's
# agents land — and it is `0` when the alerts are switched off, which would
# classify every reading saturated).

# A refusal (probe 429 / blocking window / rejected token) is a point-in-time
# verdict: trusted exactly as long as the LIMIT badge trusts one
# (`resolve_rate_limited_now` → FRESHNESS_SECONDS), then it is merely unknown.
REFUSAL_FRESHNESS_SECONDS = FRESHNESS_SECONDS
# The 5h window can fully reset inside MAX_READING_AGE_SECONDS, so its figure
# is only evidence about the next minute while display-fresh. The 7d figure is
# a LOWER BOUND for the whole bound: utilization is monotonic within the
# provider's fixed window (measured in ent#434), so age only makes it
# optimistic, never pessimistic.
FAST_WINDOW_FRESHNESS_SECONDS = FRESHNESS_SECONDS
# Snapshots do not move during a storm (no probe on this path; ambient refresh
# is >= 15 min), while `agent_count` — the only key that DOES move as switches
# land — would otherwise be the last tiebreak. Banding the headroom key lets
# load-balance spread twenty agents across two subscriptions at 39.0% and
# 39.4% instead of piling them onto one. Ten points is signal; less is noise
# beside load.
HEADROOM_BAND_PCT = 10


@dataclass(frozen=True)
class WindowReading:
    """One provider window, post-gate."""
    utilization_pct: Optional[float]   # None = the provider sent no figure
    blocked: bool                      # status outside NON_BLOCKING_WINDOW_STATUSES (None = not blocked)
    resets_at: Optional[str]


@dataclass(frozen=True)
class HeadroomReading:
    """What the last probe said, once the snapshot has passed the usability gate."""
    age_seconds: int
    provider_status: str               # "ok" | "rate_limited" | "invalid_token"
    five_hour: Optional[WindowReading]
    seven_day: Optional[WindowReading]

    @property
    def refusing(self) -> bool:
        """The provider is refusing this token right now, in some window."""
        if self.provider_status != "ok":
            return True
        return any(w.blocked for w in (self.five_hour, self.seven_day) if w is not None)


def headroom_reading(
    headroom: Optional[SubscriptionHeadroom], *, max_age_seconds: Optional[int] = None
) -> Optional[HeadroomReading]:
    """THE usability gate — `None` means "nothing here can be believed".

    Unusable: no snapshot, no age or over-age (`max_age_seconds`, default
    FRESHNESS_SECONDS), or a probe that produced no verdict at all (`error`,
    `no_windows`, any status this code does not know). A `rate_limited` or
    `invalid_token` probe DID learn something and passes through as a reading;
    what it means is each consumer's policy. Freshness is judged BEFORE status
    — a stale refusal is not a refusal — the order `classify_headroom` has
    always used. Figures pass through untouched (the classifier's verdicts
    must stay byte-identical); the ranker sanitises non-finite ones.
    """
    if headroom is None:
        return None
    age = headroom.snapshot_age_seconds
    limit = FRESHNESS_SECONDS if max_age_seconds is None else max_age_seconds
    if age is None or age > limit:
        return None
    if headroom.status not in ("ok", "rate_limited", "invalid_token"):
        return None

    def window(w: Optional[HeadroomWindow]) -> Optional[WindowReading]:
        if w is None:
            return None
        return WindowReading(
            utilization_pct=w.utilization_pct,
            blocked=w.status is not None and w.status not in NON_BLOCKING_WINDOW_STATUSES,
            resets_at=w.resets_at,
        )

    return HeadroomReading(
        age_seconds=int(age),
        provider_status=headroom.status,
        five_hour=window(headroom.five_hour),
        seven_day=window(headroom.seven_day),
    )


def cached_headroom_readings(
    subscription_ids, *, max_age_seconds: Optional[int] = None
) -> Dict[str, Optional[HeadroomReading]]:
    """The selector's read: the cached snapshots of a candidate set, gated.

    ONE `MGET` — the selection runs under the per-agent switch lock, and N
    serial 1s-bounded GETs would stall the loop for N seconds at exactly the
    moment a fleet-wide wall makes N large. NEVER probes: a probe on the
    switch path would turn every fleet-wide failure into a probe storm on the
    survivors. Tri-state like `_read_snapshot` (learnings 2026-08-19): a client
    that cannot be built, or a Redis that raises, reads every candidate as
    unknown in ONE attempt; a malformed snapshot blinds only its own
    candidate. The default bound is the SELECTION bound, not the display one.
    """
    ids = [str(s) for s in subscription_ids]
    readings: Dict[str, Optional[HeadroomReading]] = {sid: None for sid in ids}
    if not ids:
        return readings
    limit = MAX_READING_AGE_SECONDS if max_age_seconds is None else max_age_seconds
    r = get_breaker_redis()
    if r is None:
        return readings
    try:
        raws = r.mget([_SNAPSHOT_KEY.format(sid=sid) for sid in ids])
    except Exception as e:  # noqa: BLE001 — Redis could not be asked: unknown, never an error
        logger.warning(
            "[#2409] headroom snapshots unreadable (%s); ranking on load order",
            type(e).__name__,
        )
        return readings
    for sid, raw in zip(ids, raws or []):
        if not raw:
            continue
        try:
            readings[sid] = headroom_reading(_to_model(json.loads(raw)), max_age_seconds=limit)
        except Exception as e:  # noqa: BLE001 — one bad snapshot must not blind the set
            logger.warning(
                "[#2409] headroom snapshot for subscription %s is malformed (%s); "
                "treating it as unknown",
                sid, type(e).__name__,
            )
            readings[sid] = None
    return readings


SELECTION_MEASURED = "measured"   # fresh, serving, weekly figure present — ranked by nearest wall
SELECTION_UNKNOWN = "unknown"     # nothing usable — today's order (load, then name)
SELECTION_REFUSED = "refused"     # the provider is refusing it right now — not a candidate


def _finite_pct(w: Optional[WindowReading]) -> Optional[float]:
    """A usable utilization figure, or None. Two-sided: finite AND non-negative.
    A negative figure is malformed provider data, not a very empty
    subscription — unbounded it would band to -1 and sort AHEAD of a genuinely
    empty one. No upper bound: an overage window legitimately reads past 100%.
    """
    if w is None or w.utilization_pct is None:
        return None
    try:
        v = float(w.utilization_pct)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v >= 0 else None


def selection_verdict(reading: Optional[HeadroomReading]) -> tuple:
    """`(tier, primary_pct, other_pct)` — the ranker's whole opinion of one reading.

    Primary is the FULLER window: the nearest wall. The #792 retry re-issues
    the failed turn on the destination immediately, so a subscription at
    7d 20% / 5h 98% fails within the minute — "how long will it last" is the
    weekly question, "will it work now" is the 5h one, and the wall you hit
    first is whichever is closer. A lexicographic 7d-then-5h key never reaches
    its second term (1-decimal utilization never ties), which is why this is a
    max, not a tiebreak. A serving reading with no weekly figure (a 5h-only
    snapshot) is UNKNOWN — the weekly figure is the AC's key; without it
    nothing is ranked. No threshold anywhere — see the module note above.
    """
    if reading is None:
        return SELECTION_UNKNOWN, None, None
    if reading.refusing:
        if reading.age_seconds <= REFUSAL_FRESHNESS_SECONDS:
            return SELECTION_REFUSED, None, None
        return SELECTION_UNKNOWN, None, None
    week = _finite_pct(reading.seven_day)
    if week is None:
        return SELECTION_UNKNOWN, None, None
    day = (
        _finite_pct(reading.five_hour)
        if reading.age_seconds <= FAST_WINDOW_FRESHNESS_SECONDS
        else None
    )
    if day is not None and day > week:
        return SELECTION_MEASURED, day, week
    return SELECTION_MEASURED, week, day


def selection_sort_key(candidate, reading: Optional[HeadroomReading]) -> tuple:
    """Lower sorts first: `(tier, band, agent_count, primary, other, name)`.

    Inside a band the agent count decides (the storm argument on
    HEADROOM_BAND_PCT); inside UNKNOWN the key is exactly today's order —
    `agent_count ASC, name ASC` — so a set with no readings behaves as it
    always did, whatever order it arrived in. A missing "other" window sorts
    after any known one.
    """
    tier, primary, other = selection_verdict(reading)
    agents = int(getattr(candidate, "agent_count", 0) or 0)
    name = str(getattr(candidate, "name", "") or "")
    if tier == SELECTION_MEASURED:
        band = int(primary // HEADROOM_BAND_PCT)
        # `inf`, not 100: an overage window can read past 100%, and a missing
        # figure must still sort after every known one.
        return (0, band, agents, primary, math.inf if other is None else other, name)
    return (1, 0, agents, 0.0, 0.0, name)


def rank_subscriptions(candidates, readings: Dict[str, Optional[HeadroomReading]]) -> list:
    """Order the survivors of the failure filter; drop the ones the provider is
    refusing right now.

    Pure: a stable sort over `selection_sort_key`, so a set with no readings
    comes back in today's order. Never fewer than the input minus fresh
    refusals — ranking IMPROVES a choice; it never prevents one except when
    every candidate is refusing. That refusal filter is the one deliberate
    exception to #2409's literal AC #3 (recorded
    on the issue): moving an agent onto a subscription the provider is
    refusing is a guaranteed failed turn plus a failure row, and with several
    such subscriptions the agent walks through all of them (#444's class).
    """
    kept = [
        c for c in candidates
        if selection_verdict(readings.get(c.id))[0] != SELECTION_REFUSED
    ]
    return sorted(kept, key=lambda c: selection_sort_key(c, readings.get(c.id)))


def describe_reading(reading: Optional[HeadroomReading]) -> Dict[str, Any]:
    """The notification / activity / log shape — tiers and figures, never a token."""
    tier, _primary, _other = selection_verdict(reading)
    week = reading.seven_day if reading else None
    day = reading.five_hour if reading else None
    return {
        "tier": tier,
        "seven_day_pct": _finite_pct(week),
        "five_hour_pct": _finite_pct(day),
        "seven_day_resets_at": week.resets_at if week else None,
        "five_hour_resets_at": day.resets_at if day else None,
        "reading_age_seconds": reading.age_seconds if reading else None,
    }


async def ensure_reading(
    subscription_id: str, *, max_age_seconds: int
) -> tuple:
    """ONE probe decision per subscription per cycle — returns `(headroom, probed)`.

    The background loop calls this exactly once per subscription and hands the
    SAME reading to every consumer. That matters because `_probe_floor_ok`
    bounds probes at 60s per subscription: two consumers each deciding for
    themselves means the second one is floored out and reads "no data" on
    precisely the subscriptions the first one just refreshed.

    Ordering with #447 is load-bearing and lives at the call site: recovery
    runs FIRST, so for a believed-limited subscription its probe has already
    written a zero-age snapshot by the time this is called, and this returns
    that reading without spending a second probe. For everything else recovery
    is a no-op and this one does the sampling.

    Fail-CLOSED on Redis, exactly like `recover_probe`: a client object
    existing is not Redis being reachable (learnings 2026-08-19), and without a
    readable cache the result could not be stored for any surface to see, so it
    would be quota spent for nothing.
    """
    redis_ok, snapshot = _read_snapshot(subscription_id)
    if not redis_ok:
        return None, False

    age = _snapshot_age_seconds(snapshot) if snapshot else None
    stale = snapshot is None or age is None or age > max_age_seconds
    if stale and _probe_floor_ok(subscription_id, snapshot):
        fresh = await _locked_probe(subscription_id)
        if fresh is not None:
            return _to_model(fresh), True
        # `None` = single-flight contention or no resolvable token. Fall
        # through and serve whatever we already had; the classifier decides
        # whether that is still assessable.
    return _to_model(snapshot), False


def resolve_rate_limited_now(
    *, db_says_limited: bool, headroom: Optional[SubscriptionHeadroom]
) -> bool:
    """THE one `rate_limited_now` derivation (#2157 one-gate rule), three-state.

    #447: this used to be ``db_predicate OR fresh_provider_verdict`` — an OR, so
    a fresh, authoritative "allowed, 32% used, resets 19:10" straight from the
    provider was structurally **powerless** to clear the badge. The db predicate
    is "a failure row exists in the last 2 hours" and there is no success path
    that clears it (``clear_rate_limit_events`` has had zero production callers
    since #444 removed the one call, because clearing was destroying
    auto-switch's detection signal). So a subscription that recovered kept
    reporting `LIMIT` for up to two hours while its agents answered normally —
    observed live on a real fleet.

    The db predicate is an **inference from past failures**; a fresh probe is
    **ground truth about now**. So a fresh verdict wins in BOTH directions, and
    the inference is only consulted when the provider could not answer:

        fresh provider says limited   -> True   (unchanged)
        fresh provider says allowed   -> False  (#447: the missing arm)
        no usable provider verdict    -> the 2h db predicate (unchanged)

    Scope note: this is the DISPLAY predicate. Auto-switch candidate filtering
    reads ``has_recent_subscription_failures`` (kind-blind, separate by #2352),
    so nothing here can reintroduce #444's ping-pong — a subscription that just
    recovered is still skipped as a switch target until its failures age out.
    """
    if _headroom_indicates_limited(headroom):
        return True
    if _headroom_indicates_healthy(headroom):
        return False
    return bool(db_says_limited)


async def decorate_usage(usage: SubscriptionUsage, *, force: bool = False) -> SubscriptionUsage:
    """Attach the provider snapshot + finalize `rate_limited_now` via the ONE
    derivation every surface consumes (``resolve_rate_limited_now``).
    ``force`` = operator click (probe now, floored)."""
    headroom = await get_headroom(usage.subscription_id, force=force)
    if headroom is not None and headroom.status == "ok" and (
        headroom.five_hour or headroom.seven_day
    ):
        usage.source = "anthropic"
    usage.headroom = headroom
    usage.rate_limited_now = resolve_rate_limited_now(
        db_says_limited=bool(usage.rate_limited_now), headroom=headroom
    )
    return usage


# ent#433 — the ONE window→granularity policy. Hour buckets past 7 days would
# return up to 720 rows for a chart nobody reads at that resolution, so 30d
# steps down to days. `expected_buckets` is the denominator for coverage.
HISTORY_WINDOWS = {
    "24h": {"hours": 24, "bucket": "hour", "expected_buckets": 24},
    "7d": {"hours": 168, "bucket": "hour", "expected_buckets": 168},
    "30d": {"hours": 720, "bucket": "day", "expected_buckets": 30},
}


def get_history(subscription_id: str, window: str) -> dict:
    """Bounded headroom series for one subscription (ent#433).

    Read-only and synchronous — this touches no provider and spawns no probe.
    That is deliberate and must stay true: history records what already
    happened, so viewing a trend can never cost subscription quota, and a
    sparse chart must never become a reason to probe more often.

    `coverage_pct` is buckets-observed ÷ buckets-elapsed. It is what lets a
    consumer say "we watched 12% of this week" instead of drawing a confident
    line through near-total darkness.

    Raises KeyError for an unknown window; the router validates first.
    """
    spec = HISTORY_WINDOWS[window]
    buckets = db.get_headroom_history(
        subscription_id, hours=spec["hours"], bucket=spec["bucket"]
    )
    expected = spec["expected_buckets"]
    coverage = round(100.0 * len(buckets) / expected, 1) if expected else 0.0
    return {
        "subscription_id": subscription_id,
        "window": window,
        "bucket": spec["bucket"],
        "buckets": buckets,
        # A burst of probes can leave more distinct buckets than the nominal
        # count only if clocks disagree; clamp so the figure is never absurd.
        "coverage_pct": min(coverage, 100.0),
    }


async def pressure_states(
    subscription_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Per-subscription pressure for the fleet batch endpoint: 24h event counts
    (total AND the auth split) + the same one-gate `rate_limited_now`
    derivation + headroom summary.

    #2352: ``auth_failures_24h`` rides alongside the total because the total
    cannot distinguish them, and after the predicate split a subscription whose
    token is dead is no longer `rate_limited_now` — without the split the badge
    would simply trade one wrong word ("limit") for another ("429s").
    """
    counts_24h = db.get_failure_event_counts_by_subscription(hours=24)
    out: Dict[str, Dict[str, Any]] = {}
    for sid in subscription_ids:
        # wait=False: the dashboard's 60s poll is the DEMAND signal for a
        # refresh, never a caller that blocks on one.
        headroom = await get_headroom(sid, wait=False)
        # Same one-gate resolver as `decorate_usage` — the per-agent chip and
        # the tile must never disagree about one subscription (#447).
        limited = resolve_rate_limited_now(
            db_says_limited=db.is_subscription_rate_limited(sid), headroom=headroom
        )
        entry = counts_24h.get(sid) or {}
        by_kind = entry.get("by_kind") or {}
        out[sid] = {
            "failure_events_24h": int(entry.get("total", 0)),
            "auth_failures_24h": int(by_kind.get("auth", 0)),
            "rate_limited_now": bool(limited),
            "headroom": headroom,
        }
    return out
