"""
Agent self-reminder create service (#1296).

Thin orchestration for the reminder create path (Invariant #1 — the router holds
no business logic; idempotency + response headers stay in the router, everything
below is create-time business logic):

- resolve the relative/absolute fire spec to an absolute ISO-Z instant,
- enforce the min/max delay window (400),
- clamp ``timeout_seconds`` to the agent's execution cap (400, #929 parity),
- enforce the pending-count and durable rolling-24h create caps (429),
- resolve provenance, and delegate persistence to ``db.insert_reminder``.

List/cancel are thin ``db`` calls (no service needed).
"""

from datetime import timedelta
from typing import Dict, Optional

from fastapi import HTTPException, status

from database import db
from models import (
    ReminderCreate,
    REMINDER_MIN_DELAY_SECONDS,
    REMINDER_MAX_DELAY_SECONDS,
    MAX_PENDING_REMINDERS_PER_AGENT,
    MAX_REMINDERS_PER_AGENT_PER_DAY,
)
from utils.helpers import utc_now, to_utc_iso, parse_iso_timestamp, iso_cutoff


def _resolve_fire_at(data: ReminderCreate):
    """Resolve the (validated-XOR) fire spec to a tz-aware UTC datetime."""
    now = utc_now()
    if data.delay_seconds is not None:
        return now + timedelta(seconds=data.delay_seconds)
    # fire_at already validated ISO-8601 by ReminderCreate; parse to tz-aware UTC.
    return parse_iso_timestamp(data.fire_at)


def create_reminder(
    agent_name: str,
    data: ReminderCreate,
    *,
    owner_id: Optional[int],
    created_by_email: Optional[str],
    source_agent_name: Optional[str] = None,
    source_mcp_key_id: Optional[str] = None,
) -> Dict:
    """Validate bounds, resolve the fire instant, and insert a pending reminder.

    Raises ``HTTPException`` (400/429) on a bound violation BEFORE inserting, so
    the router can release its idempotency claim; on success returns the full
    reminder dict. Never called on an idempotent replay (the router short-circuits).
    """
    now = utc_now()
    fire_dt = _resolve_fire_at(data)

    # Min/max delay window — enforced against the RESOLVED instant so both the
    # relative and absolute paths get the same guarantee.
    delta_seconds = (fire_dt - now).total_seconds()
    if delta_seconds < REMINDER_MIN_DELAY_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "reminder_fire_too_soon",
                "message": (
                    f"Reminder must fire at least {REMINDER_MIN_DELAY_SECONDS}s "
                    f"from now (got {int(delta_seconds)}s)."
                ),
                "min_delay_seconds": REMINDER_MIN_DELAY_SECONDS,
            },
        )
    if delta_seconds > REMINDER_MAX_DELAY_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "reminder_fire_too_far",
                "message": (
                    f"Reminder must fire within {REMINDER_MAX_DELAY_SECONDS}s "
                    f"(got {int(delta_seconds)}s)."
                ),
                "max_delay_seconds": REMINDER_MAX_DELAY_SECONDS,
            },
        )

    # Timeout clamp to the agent cap (#929 parity) — a reminder must not hold a
    # max_parallel_tasks slot past the operator-set execution_timeout_seconds.
    if data.timeout_seconds is not None:
        cap = db.get_execution_timeout(agent_name)
        if data.timeout_seconds > cap:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "reminder_timeout_exceeds_agent_cap",
                    "message": (
                        f"Reminder timeout {data.timeout_seconds}s exceeds agent "
                        f"execution_timeout_seconds {cap}s. Raise the agent cap first."
                    ),
                    "agent_cap_seconds": cap,
                    "requested_seconds": data.timeout_seconds,
                },
            )

    # Concurrency cap (real DB count, NOT fail-open) → 429.
    if db.count_pending_reminders(agent_name) >= MAX_PENDING_REMINDERS_PER_AGENT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "reminder_pending_cap_reached",
                "message": (
                    f"Agent already has {MAX_PENDING_REMINDERS_PER_AGENT} pending "
                    "reminders (the concurrency cap). Cancel one or wait for one to fire."
                ),
                "max_pending": MAX_PENDING_REMINDERS_PER_AGENT,
            },
        )

    # Durable rolling-24h create cap (the non-fail-open self-perpetuation
    # backstop) → 429.
    since = iso_cutoff(hours=24)
    if db.count_reminders_created_since(agent_name, since) >= MAX_REMINDERS_PER_AGENT_PER_DAY:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "reminder_daily_cap_reached",
                "message": (
                    f"Agent has created {MAX_REMINDERS_PER_AGENT_PER_DAY} reminders in "
                    "the last 24h (the durable rate cap)."
                ),
                "max_per_day": MAX_REMINDERS_PER_AGENT_PER_DAY,
            },
        )

    return db.insert_reminder(
        agent_name,
        data.message,
        to_utc_iso(fire_dt),
        model=data.model,
        timeout_seconds=data.timeout_seconds,
        allowed_tools=data.allowed_tools,
        owner_id=owner_id,
        created_by_email=created_by_email,
        source_agent_name=source_agent_name,
        source_mcp_key_id=source_mcp_key_id,
    )
