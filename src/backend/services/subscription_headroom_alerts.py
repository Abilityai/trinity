"""Weekly-subscription-headroom alerts (abilityai/trinity-enterprise#434).

Tells the operator, before the wall, that a subscription's **7-day** window is
running out — while there is still time to stagger schedules, move agents, or
add a subscription. Two tiers: a per-subscription warning, and a fleet
escalation for the case the operator actually fears, every subscription full
at once, where SUB-003 auto-switch has nowhere useful to go.

## Why there is no durable state machine here

The issue specified an edge-trigger plus a hysteresis floor plus a re-arm on
window reset, all persisted. Measuring the provider first deleted most of it.

`subscription_headroom_history` on a live instance shows `seven_day_resets_at`
holding CONSTANT at one midnight-UTC instant across five days of probes while
utilization climbed 36 → 90, then STEPPING exactly +7 days. So the weekly
window is **fixed-with-reset, not rolling**, and utilization is monotonic
non-decreasing within a window. (`docs/memory/feature-flows/dashboard-grid-view.md`
described these as rolling windows; that claim is corrected as part of this
change.)

Two consequences, both of which remove work:

1. A hysteresis floor is dead code — utilization does not fall inside a window,
   so the only real re-arm is the reset.
2. `resets_at` therefore IS the window's identity, and putting it in the alert
   id makes the id the entire state machine:

       sub-headroom-{sid}-{reset-day}-{tier}

   `db.create_operator_queue_item` maps `item["id"]` onto `request_id`, which
   is `UNIQUE(agent_name, request_id)` with ON CONFLICT DO NOTHING. So the
   same window re-emits into the same row (no duplicate), a reset mints a new
   id (re-armed), and the escalation carries its own tier suffix. Cross-worker
   and cross-restart dedup fall out for free, with no lock and no memo.

The id is quantised to the DAY rather than the exact instant. Under the
measured fixed-window semantics that is exactly one episode per window; if a
different provider plan ever did behave as rolling, it degrades to at most one
alert per day instead of one per probe. The cheap belt is worth more than the
precision.

## Residual, stated rather than hidden

`create_item` has no UPDATE path, so a warning row keeps the number it was
raised with — a 75% alert still reads 75% when the subscription later sits at
92%. Same residual `retention_guard` documents for its own alarm. The
escalation is a SEPARATE id with a self-contained body, so the newer figure
does arrive; it just arrives as a second item rather than an edit.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

from database import db
from services.subscription_headroom_service import (
    HAS_HEADROOM,
    SATURATED,
    UNASSESSABLE,
)
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# The alarm host. Uncreatable as a real agent — `sanitize_agent_name` strips a
# leading underscore — and registered in `canary/snapshot.py`'s
# `_PLATFORM_ALARM_SENTINELS` so L-03 does not report it as a ghost agent.
ALARM_AGENT_NAME = "_sub-headroom"
# Registered in `operator_queue_service._RESERVED_ID_PREFIXES`, so an agent
# cannot pre-create one of these ids and on-conflict-silence its own alarm.
ALARM_ID_PREFIX = "sub-headroom-"

THRESHOLD_SETTING = "subscription_headroom_alert_threshold_pct"

# Operator-ruled default: the feature is ON out of the box.
DEFAULT_THRESHOLD_PCT = 75
# 0 disables the alerts entirely (the `operator_queue_retention_days` idiom).
# Below 50 a weekly window is barely started and every fleet would alarm; at
# 100 the alert is unreachable given the provider's 1-decimal rounding.
MIN_THRESHOLD_PCT = 50
MAX_THRESHOLD_PCT = 99

# The provider's weekly window, established by the measurement above.
WINDOW_SECONDS = 7 * 24 * 3600

# A fleet claim needs at least two subscriptions to mean anything: with one,
# "this subscription is full" and "every subscription is full" are the same
# fact, and shipping both would be two operator items for one event.
MIN_FLEET_SIZE = 2

# How old a reading may be and still be classified. Deliberately longer than
# the display predicates' FRESHNESS_SECONDS: a 7-day window does not move
# meaningfully in a couple of hours, whereas a badge showing a stale number
# is wrong immediately.
#
# It bounds EVERY classification, not only the fleet arm, and the fleet arm is
# why it has to be generous: that claim needs every subscription simultaneously
# assessable, which decays exponentially with fleet size (at a 2% per-probe
# failure rate and N=50, all-fresh is a 36% event), so a tight bound would make
# the alert unreachable on exactly the large fleet it matters for.
MAX_READING_AGE_SECONDS = 2 * 3600

# No operator item is worth a storm. If a whole fleet crosses at once the
# fleet alert carries the story; the per-subscription ones are capped.
MAX_PER_SUBSCRIPTION_ALERTS_PER_CYCLE = 5

TIER_WARN = "warn"
TIER_CRIT = "crit"

_SID_SAFE = re.compile(r"[^A-Za-z0-9._-]")


# ---------------------------------------------------------------------------
# Configuration — one reader, fail-safe in the conservative direction
# ---------------------------------------------------------------------------


def effective_threshold_pct() -> int:
    """The one reader for the alert threshold (the `effective_backup_retention_days`
    shape: garbage never becomes a *different working value*).

    Coerces unparseable or out-of-range input to the default rather than to 0
    or 100, because for a threshold the two failure directions are not
    symmetric: 0 would alarm on every subscription forever and 100 would
    silently disable the feature. `0` is honoured only when it is explicitly
    stored — that is the documented "off" value, not a parse artifact.
    """
    try:
        raw = db.get_setting_value(THRESHOLD_SETTING, default=None)
    except Exception as e:  # noqa: BLE001 — a settings blip must not kill the sweep
        logger.warning("headroom alert threshold unreadable (%s); using default", e)
        return DEFAULT_THRESHOLD_PCT
    if raw is None or str(raw).strip() == "":
        return DEFAULT_THRESHOLD_PCT
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning(
            "headroom alert threshold %r is not a number; using default %d",
            raw, DEFAULT_THRESHOLD_PCT,
        )
        return DEFAULT_THRESHOLD_PCT
    if value == 0:
        return 0
    if value < MIN_THRESHOLD_PCT or value > MAX_THRESHOLD_PCT:
        logger.warning(
            "headroom alert threshold %d out of range %d-%d; using default %d",
            value, MIN_THRESHOLD_PCT, MAX_THRESHOLD_PCT, DEFAULT_THRESHOLD_PCT,
        )
        return DEFAULT_THRESHOLD_PCT
    return value


def escalation_pct(threshold_pct: int) -> int:
    """DERIVED, never a second knob.

    Two independently-settable thresholds are an oscillator waiting to happen:
    a fixed 90 escalation under a threshold of 95 fires below the warning, and
    a fixed floor above the threshold re-arms on every cycle. `validate_ops_setting`
    is per-key and structurally cannot express a cross-field invariant, so the
    only way to make the pair un-misconfigurable is to not have a pair.

    **Consequence worth knowing: at a threshold of 90 or above the two bounds
    coincide and the warning tier is unreachable** — `decide_tier` tests
    `>= escalate_at` first, so every crossing files as `crit`/`high`. That is
    the correct behaviour (at 90%+ of a weekly window there is no "gentle"
    tier left to offer) but it is a real behaviour change across the 90
    boundary and nothing else announces it.
    """
    return max(threshold_pct, 90)


# ---------------------------------------------------------------------------
# The projection — how urgent, decided from data the snapshot already carries
# ---------------------------------------------------------------------------


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def project_end_utilization(
    utilization_pct: Optional[float],
    resets_at: Optional[str],
    *,
    now: Optional[datetime] = None,
) -> Optional[float]:
    """Where this window lands at the current burn rate, or `None` if unknowable.

    This is what replaces "alert if the reset is less than N hours away". There
    is no such constant and there is no setting for one: the window length is
    known, `resets_at` says how much of it is left, so how far through the week
    we are — and therefore the operator's own pace — is already derivable.

        projected_end = utilization_pct / fraction_of_window_elapsed

    It self-calibrates. 75% at day 3 projects to 175% and is an emergency; 75%
    with hours left projects to ~77% and is a normal week. A heavy user and a
    light user are each judged against their own rate rather than a shared
    clock.

    Linear burn is an assumption, and it is unstable when very little of the
    window has elapsed. It needs no separate guard here because the caller only
    ever projects a reading that already crossed the threshold — you cannot be
    at 75% of a week early in that week without genuinely being in trouble.
    """
    if utilization_pct is None:
        return None
    reset_dt = _parse_iso(resets_at)
    if reset_dt is None:
        return None
    current = now or datetime.now(timezone.utc)
    remaining = (reset_dt - current).total_seconds()
    # A lapsed or absurd reset instant tells us nothing about pace.
    if remaining <= 0 or remaining >= WINDOW_SECONDS:
        return None
    elapsed_fraction = 1.0 - (remaining / WINDOW_SECONDS)
    if elapsed_fraction <= 0:
        return None
    return utilization_pct / elapsed_fraction


# ---------------------------------------------------------------------------
# Decisions — pure
# ---------------------------------------------------------------------------


def episode_key(resets_at: Optional[str], *, now: Optional[datetime] = None) -> str:
    """The window's identity, quantised to the day. See the module docstring."""
    reset_dt = _parse_iso(resets_at)
    if reset_dt is not None:
        return reset_dt.date().isoformat()
    current = now or datetime.now(timezone.utc)
    return f"unknown-{current.date().isoformat()}"


def alert_id(subscription_id: str, episode: str, tier: str) -> str:
    safe_sid = _SID_SAFE.sub("-", str(subscription_id))[:64]
    return f"{ALARM_ID_PREFIX}{safe_sid}-{episode}-{tier}"


def fleet_alert_id(episode: str) -> str:
    return f"{ALARM_ID_PREFIX}fleet-{episode}"


def decide_tier(
    utilization_pct: Optional[float], threshold_pct: int, escalate_at: int
) -> Optional[str]:
    """Highest tier reached, or `None` when the subscription is below threshold.

    Exactly ONE tier per subscription per cycle: a reading that jumps straight
    from below-threshold to 95% satisfies both conditions, and emitting both
    would be two operator items for one crossing.
    """
    if utilization_pct is None:
        return None
    if utilization_pct >= escalate_at:
        return TIER_CRIT
    if utilization_pct >= threshold_pct:
        return TIER_WARN
    return None


def priority_for(tier: str, projected_end: Optional[float]) -> str:
    """Fire at the threshold always; let the projection say how much it matters.

    The operator asked to be told at the threshold, so the alert is never
    withheld. What the projection decides is urgency: at the threshold but on
    track to finish the week under 100%, there is nothing to act on, so it is
    filed low rather than suppressed.

    An unknowable projection is treated as not-on-pace: a missing `resets_at`
    is not evidence of an emergency, and inventing one would make the priority
    ladder dishonest in the loud direction.
    """
    if tier == TIER_CRIT:
        return "high"
    if projected_end is not None and projected_end >= 100.0:
        return "high"
    return "low"


def fleet_verdict(classifications: Dict[str, str]) -> Dict[str, Any]:
    """Is EVERY subscription saturated — and may we honestly say so?

    A positive fleet-wide claim needs positive evidence from every member (the
    ent#100 rule), so a single unassessable subscription blocks the claim and
    is named instead of being silently counted either way.
    """
    sids = sorted(classifications)
    unassessable = [s for s in sids if classifications[s] == UNASSESSABLE]
    saturated = [s for s in sids if classifications[s] == SATURATED]
    has_room = [s for s in sids if classifications[s] == HAS_HEADROOM]

    if len(sids) < MIN_FLEET_SIZE:
        reason = "single_subscription" if sids else "no_subscriptions"
    elif unassessable:
        reason = "unassessable_members"
    elif has_room:
        reason = "headroom_available"
    else:
        reason = None

    return {
        "saturated": reason is None,
        "blocked_reason": reason,
        "saturated_ids": saturated,
        "has_headroom_ids": has_room,
        "unassessable_ids": unassessable,
    }


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def _fmt_pct(value: Optional[float]) -> str:
    return "unknown" if value is None else f"{value:.0f}%"


def _fmt_reset(resets_at: Optional[str]) -> str:
    """`resets_at` is Optional and the parse can fail; say so rather than
    rendering a blank (the ent#259 `reset unknown` precedent)."""
    dt = _parse_iso(resets_at)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "reset unknown"


def _emit(item_id: str, *, title: str, question: str, priority: str,
          context: Dict[str, Any]) -> bool:
    """One platform create on the sentinel host. Never raises.

    Platform-only by construction: an agent cannot drive the VOLUME. The
    cadence is the sweep's, the id is deterministic per (subscription, window,
    tier) so a re-emit is an on-conflict no-op, and the per-cycle cap bounds a
    whole fleet crossing at once. That is what makes it a direct create with an
    allowlist entry in `tests/unit/test_1677_operator_alert_emitters.py` rather
    than a `create_bounded_alert` caller.

    Note the precise claim: agent-chosen *names* do reach the body (an agent may
    spawn children and name them), but they arrive sanitized, are capped at five
    per alert, and already appear on every operator surface. It is the volume,
    not the absence of agent-derived text, that justifies the exemption.
    """
    item = {
        "id": item_id,
        "type": "alert",
        "status": "pending",
        "priority": priority,
        "title": title,
        "question": question,
        "context": context,
        "created_at": utc_now_iso(),
        # Must stay None: `mark_operator_queue_expired` flips any pending row
        # past `expires_at` to expired fleet-wide every 5s.
        "expires_at": None,
    }
    try:
        db.create_operator_queue_item(ALARM_AGENT_NAME, item)
        logger.warning("[headroom-alert] %s", title)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("[headroom-alert] failed to emit %s", item_id)
        return False


def emit_subscription_alert(
    *,
    subscription_id: str,
    subscription_name: Optional[str],
    tier: str,
    utilization_pct: Optional[float],
    projected_end: Optional[float],
    resets_at: Optional[str],
    threshold_pct: int,
    agents: Sequence[str],
    now: Optional[datetime] = None,
) -> bool:
    """One subscription crossed its weekly threshold.

    The body is self-contained. A warning row cannot be edited later (see the
    module docstring), so the escalation must stand on its own rather than
    assume the operator reads it beside the earlier item.
    """
    label = subscription_name or subscription_id
    episode = episode_key(resets_at, now=now)
    on_pace = projected_end is not None and projected_end >= 100.0

    if tier == TIER_CRIT:
        headline = f"Subscription '{label}' is at {_fmt_pct(utilization_pct)} of its weekly limit"
    else:
        headline = f"Subscription '{label}' passed {threshold_pct}% of its weekly limit"

    lines = [
        f"7-day window: {_fmt_pct(utilization_pct)} used. Resets {_fmt_reset(resets_at)}.",
    ]
    if projected_end is not None:
        if on_pace:
            lines.append(
                f"At the current rate this window finishes around "
                f"{_fmt_pct(projected_end)} — it will run out before it resets."
            )
        else:
            lines.append(
                f"At the current rate this window finishes around "
                f"{_fmt_pct(projected_end)}, so it should last until the reset."
            )
    else:
        lines.append("Not enough information to project where this window lands.")
    if agents:
        shown = list(agents)[:5]
        more = len(agents) - len(shown)
        suffix = f" (+{more} more)" if more > 0 else ""
        lines.append(f"Agents on this subscription: {', '.join(shown)}{suffix}.")
    else:
        lines.append("No agents are currently assigned to this subscription.")

    return _emit(
        alert_id(subscription_id, episode, tier),
        title=headline,
        question="\n".join(lines),
        priority=priority_for(tier, projected_end),
        context={
            "subscription_id": subscription_id,
            "subscription_name": label,
            "tier": tier,
            "utilization_pct": utilization_pct,
            "projected_end_pct": projected_end,
            "threshold_pct": threshold_pct,
            "resets_at": resets_at,
            "agent_count": len(agents),
            "on_pace_to_exhaust": on_pace,
        },
    )


def emit_fleet_alert(
    *,
    verdict: Dict[str, Any],
    names: Dict[str, str],
    threshold_pct: int,
    earliest_reset: Optional[str],
    now: Optional[datetime] = None,
) -> bool:
    """Every subscription is saturated — auto-switch has nowhere better to go.

    Deliberately narrower than the issue's wording. The claim is about what was
    MEASURED (every registered subscription is at or past the threshold), not
    about what auto-switch will do: `select_best_alternative_subscription`
    filters on recent failures and reads no headroom at all, so a sentence
    about its behaviour would be underived. Teaching it about headroom is
    tracked separately at abilityai/trinity#2409.
    """
    saturated = verdict.get("saturated_ids") or []
    labels = [names.get(s, s) for s in saturated]
    episode = episode_key(earliest_reset, now=now)

    lines = [
        f"All {len(saturated)} registered subscriptions are at or past "
        f"{threshold_pct}% of their weekly limit: {', '.join(labels)}.",
        f"Earliest reset: {_fmt_reset(earliest_reset)}.",
        "There is no subscription with meaningful headroom left to move agents onto.",
    ]
    return _emit(
        fleet_alert_id(episode),
        title=f"All {len(saturated)} subscriptions are near their weekly limit",
        priority="high",
        question="\n".join(lines),
        context={
            "saturated_ids": saturated,
            "subscription_count": len(saturated),
            "threshold_pct": threshold_pct,
            "earliest_reset_at": earliest_reset,
        },
    )
