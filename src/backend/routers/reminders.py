"""
Agent self-reminder API (#1296).

An agent schedules a durable one-shot deferred self-trigger via the MCP
``set_reminder`` tool, which POSTs here. All three endpoints are agent-nested and
**self-gated** (AC #5): ``AuthorizedAgent`` checks the key owner can access the
path agent, but does NOT stop an agent-scoped key from managing a *sibling* agent
the owner also accesses — so each endpoint additionally requires an agent-scoped
caller's bound ``agent_name`` to equal the path agent (the exact reports/heartbeat
self-gate). Connector-scoped keys are rejected (``_reject_connector_principal`` +
reminders are deliberately OFF the connector auth-entry allowlist): a connector
key is consumption-only and must not schedule future budget-consuming executions.

Every by-id operation is tenant-scoped in the DB layer (``agent_name`` in the
predicate), so a reminder id belonging to another agent is a uniform 404, never a
200/404 oracle.

Idempotency (Invariant #18): create routes through ``idempotency_service`` scoped
per agent. A caller ``Idempotency-Key`` header wins; absent, the backend derives a
key over the RAW input (message + literal fire spec), so a naive retry dedupes.
Terminal (cancelled/fired) stored rows are excluded from replay — a
cancel-then-recreate-identical yields a fresh pending reminder.
"""

import logging
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse

from database import db
from dependencies import (
    AuthorizedAgent,
    get_current_user,
    _reject_connector_principal,
)
from models import Reminder, ReminderCreate, ReminderSummary, User
from services import idempotency_service, rate_limiter, reminder_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["reminders"])

# Per-agent create rate limit (flood guard; shared sliding-window limiter,
# fail-open if Redis is down) — mirrors reports. The durable pending + daily
# caps in reminder_service are the non-fail-open bounds.
REMINDER_RATE_LIMIT = int(os.getenv("REMINDER_RATE_LIMIT", "30"))
REMINDER_RATE_WINDOW = 60  # seconds


def _self_gate(current_user: User, name: str) -> None:
    """AC #5 self-only auth + connector rejection (mirrors reports/heartbeat)."""
    _reject_connector_principal(current_user)
    if current_user.agent_name and current_user.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent-scoped key may only manage its own reminders",
        )


# #1806: statuses the autonomy gate can still act on. A terminal reminder is
# never "held" — it already fired, was cancelled, or gave up.
_LIVE_STATUSES = ("pending", "firing")


def _autonomy_hold(name: str, row_status: str, autonomy_enabled: Optional[bool] = None) -> bool:
    """Is this reminder live but un-armable because the agent's autonomy is off?

    #1806: the scheduler's ``get_active_reminders`` filters on
    ``ao.autonomy_enabled = 1``, so a reminder on a paused agent is never armed
    — no job, therefore no skip to log. Derived at read time (one cheap lookup,
    no column, no migration). Fail-open to "not held": a settings-read failure
    must never invent a warning on a healthy reminder.
    """
    if row_status not in _LIVE_STATUSES:
        return False
    try:
        if autonomy_enabled is None:
            autonomy_enabled = db.get_autonomy_enabled(name)
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not resolve autonomy for %s; reporting no hold", name)
        return False
    return not autonomy_enabled


def _reminder_response(row: dict) -> dict:
    """Reminder-model-shaped JSON dict (drops the non-response provenance cols)."""
    return Reminder(
        **row, autonomy_hold=_autonomy_hold(row["agent_name"], row["status"])
    ).model_dump()


@router.post("/agents/{name}/reminders", response_model=Reminder, status_code=201)
async def create_reminder_endpoint(
    data: ReminderCreate,
    name: AuthorizedAgent,
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None),
    x_source_agent: Optional[str] = Header(None),
):
    """Schedule a one-shot self-reminder (agent calls this via ``set_reminder``)."""
    _self_gate(current_user, name)

    # Flood guard (fail-open) — the durable pending/daily caps are enforced in
    # the service.
    rate_limiter.enforce(
        f"agent_reminder:{name}",
        REMINDER_RATE_LIMIT,
        REMINDER_RATE_WINDOW,
        detail="Reminder rate limit exceeded for this agent.",
    )

    # Idempotency — caller header wins, else a key over the RAW input.
    scope = idempotency_service.make_agent_scope(name)
    key = idempotency_key or idempotency_service.derive_reminder_key(
        name, data.message, data.raw_fire_spec()
    )
    idem = idempotency_service.begin(scope, key)

    terminal_replay_fresh = False
    if idem.replay:
        if idem.in_flight:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A reminder create with this Idempotency-Key is still being processed.",
            )
        stored = idem.snapshot
        existing = (
            db.get_reminder(name, stored["id"])
            if isinstance(stored, dict) and stored.get("id")
            else None
        )
        if existing and existing["status"] in ("pending", "firing"):
            # True dedup — the original reminder is still live.
            return JSONResponse(
                content=_reminder_response(existing),
                headers={"X-Idempotent-Replay": "true"},
            )
        # Stored reminder is terminal (cancelled/fired) or gone → proceed as a
        # FRESH create; upgrade the snapshot so a subsequent identical call
        # replays the NEW reminder (security F4 — a one-shot must not silently
        # no-op a cancel-then-recreate-identical).
        terminal_replay_fresh = True

    try:
        reminder = reminder_service.create_reminder(
            name,
            data,
            owner_id=current_user.id,
            created_by_email=current_user.email,
            source_agent_name=current_user.agent_name or x_source_agent,
            # #2389: the credential actually presented, never the forgeable X-MCP-Key-* headers.
            source_mcp_key_id=getattr(current_user, "mcp_key_id", None),
        )
    except HTTPException:
        # A bound failed BEFORE the row was inserted → release the fresh claim so
        # a corrected retry can proceed (no-op on a terminal-replay claim). NEVER
        # fail() after the insert (create_reminder inserts last; only a pre-insert
        # HTTPException reaches here) — a post-insert release would let a retry
        # double-insert (security F6).
        idempotency_service.fail(idem)
        raise

    snapshot = _reminder_response(reminder)
    # Finalize the idempotency row (best-effort — never raises past a completed,
    # persisted create).
    if terminal_replay_fresh:
        idempotency_service.upgrade_snapshot(scope, key, snapshot)
    else:
        idempotency_service.complete(idem, reminder["id"], snapshot)

    return Reminder(
        **reminder,
        autonomy_hold=_autonomy_hold(name, reminder["status"]),
    )


@router.get("/agents/{name}/reminders", response_model=List[ReminderSummary])
async def list_reminders_endpoint(
    name: AuthorizedAgent,
    current_user: User = Depends(get_current_user),
    status_filter: Optional[str] = Query(
        "pending",
        alias="status",
        description="Filter by status (default 'pending'); 'all' lists every status.",
    ),
):
    """List one agent's reminders (WHERE agent_name=?), soonest fire first."""
    _self_gate(current_user, name)
    effective = None if status_filter in (None, "", "all") else status_filter
    rows = db.list_reminders(name, status=effective)
    # #1806: one autonomy lookup for the whole page — every row belongs to the
    # same agent, so a per-row resolve would be a pointless N+1.
    autonomy = db.get_autonomy_enabled(name) if rows else True
    return [
        ReminderSummary(
            **r, autonomy_hold=_autonomy_hold(name, r["status"], autonomy)
        )
        for r in rows
    ]


@router.post("/agents/{name}/reminders/{reminder_id}/cancel", response_model=Reminder)
async def cancel_reminder_endpoint(
    name: AuthorizedAgent,
    reminder_id: str,
    current_user: User = Depends(get_current_user),
):
    """Cancel a pending reminder (CAS pending→cancelled, tenant-scoped).

    already-cancelled → 200 no-op; firing/fired/failed → 409; foreign/unknown id
    → uniform 404.
    """
    _self_gate(current_user, name)
    outcome = db.cancel_reminder(name, reminder_id)
    if outcome == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Reminder not found"
        )
    if outcome == "conflict":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Reminder is no longer pending and cannot be cancelled",
        )
    row = db.get_reminder(name, reminder_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Reminder not found"
        )
    return Reminder(
        **row, autonomy_hold=_autonomy_hold(name, row["status"])
    )
