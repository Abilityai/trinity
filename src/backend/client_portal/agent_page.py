"""The Workspace agent page (ent#360).

An agent had no home. A roster row started a chat, so there was nowhere to see
what an agent has been doing, nowhere for it to ask you something while no chat
is open, and nowhere to show what it can do. This assembles that page.

Two constraints shape everything here, and both are subtractive:

**It reports; it does not configure.** No schedules, no skill editing, no logs,
no costs (ent#360 AC #7). Building agents stays operator-side. Model and plan
are simply not shown — the AC allows them "informational and visibility-gated",
and the cheapest way to satisfy a gate is to not open the door.

**The viewer may be an external client, not an operator.** Every field below is
chosen with that in mind, because the same page serves a portal-token client and
a platform user. Three places where that bites:

* `recent_work` is projected down to status/trigger/time/duration. The
  underlying accessor also returns `message`, `cost`, `model_used` and
  `source_user_email` — another user's prompt, and two things AC #7 excludes.
* `asks` carries only agent-authored `approval`/`question` items, never
  `alert`. Alerts are platform-generated (sync-failing, git-bloat, breaker
  dormancy) and are operations telemetry, not something the agent is asking a
  person. `context` is never exposed at all: it is free-form agent JSON and has
  been a credential-leak surface before (canary G-04).
* Everything is DB-sourced, so the page renders for a stopped agent (AC #6) —
  degraded, not empty.
"""
from __future__ import annotations

import logging
from typing import Optional

from database import db

from . import db as portal_db

logger = logging.getLogger(__name__)

# ent#360 AC: the stats strip is a window. Same set the Agent Detail Overview
# offers (#1107), so the analytics accessor is reused rather than reimplemented.
WINDOWS = {"7d": 168, "14d": 336, "30d": 720}
DEFAULT_WINDOW = "7d"

# Only these reach a Workspace viewer. `alert` is platform-generated operations
# telemetry, not a question an agent is asking a person.
ASK_TYPES = ("approval", "question")
MAX_ASKS = 20
MAX_RECENT_WORK = 20
MAX_CHATS = 20


def _health(agent_name: str) -> dict:
    """Coarse health for the header, from the last persisted check.

    Never the live Docker state: AC #6 wants this page to render for a stopped
    agent, and a probe that needs the container is exactly what cannot answer
    then. `unknown` is an honest answer for an agent monitoring has never
    checked — monitoring is default-OFF (#1121), so on many installs that is
    every agent, and rendering "unhealthy" there would be a lie.
    """
    try:
        row = db.get_latest_health_check(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: health read failed for %s: %s", agent_name, e)
        return {"status": "unknown", "checked_at": None}
    if not row:
        return {"status": "unknown", "checked_at": None}
    return {
        "status": row.get("status") or "unknown",
        "checked_at": row.get("checked_at"),
    }


def _stats(agent_name: str, window: str) -> dict:
    """Activity chart + headline numbers, from the existing analytics accessor.

    The issue's Technical Notes name that accessor specifically, so this adds no
    query of its own: it reshapes what #1107 already computes.
    """
    hours = WINDOWS.get(window, WINDOWS[DEFAULT_WINDOW])
    try:
        a = db.get_agent_analytics(agent_name, hours)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: analytics failed for %s: %s", agent_name, e)
        return {
            "window": window, "window_hours": hours, "total_executions": 0,
            "success_rate": None, "timeline": [], "by_type": [],
            "first_try": {"terminal": 0, "first_try": 0, "rate": None},
            "unavailable": True,
        }
    return {
        "window": window,
        "window_hours": a.get("window_hours", hours),
        "total_executions": a.get("total_executions", 0),
        "success_rate": a.get("success_rate"),
        # Per-day counts for the chart. `by_type` per day is kept so the chart
        # can stack by what triggered the work, as the Overview one does.
        "timeline": a.get("timeline", []),
        "by_type": a.get("by_type", []),
        # AC #3's "first-try rate". Distinct from success_rate above, which
        # counts a retried-then-succeeded execution as a success.
        "first_try": portal_db.first_try_stats(agent_name, hours),
        "unavailable": False,
    }


def _asks(agent_name: str) -> list[dict]:
    """What this agent is waiting on a person for.

    Read-only here. Answering an approval writes to the operator queue, which is
    an operator surface with its own auth; the page offers "reply in chat"
    instead, so neither audience lands on a control that does nothing.
    """
    try:
        items = db.list_operator_queue_items(
            status="pending", agent_name=agent_name, limit=MAX_ASKS,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: operator queue read failed for %s: %s", agent_name, e)
        return []
    out = []
    for it in items:
        if it.get("type") not in ASK_TYPES:
            continue
        out.append({
            "id": it.get("id"),
            "type": it.get("type"),
            "priority": it.get("priority"),
            "title": it.get("title"),
            "question": it.get("question"),
            "options": it.get("options"),
            "created_at": it.get("created_at"),
            # `context` is deliberately absent — free-form agent JSON, and a
            # known credential-leak surface (canary G-04).
        })
    return out


def _recent_work(agent_name: str, limit: int = MAX_RECENT_WORK) -> list[dict]:
    """What the agent has been doing — shape only, no content.

    The accessor returns `message`, `cost`, `model_used` and `source_user_email`
    among others. A Workspace viewer may be an external client, so the prompt
    text of somebody else's task, the spend, and the model are all projected
    away here rather than filtered in the UI: a field that never leaves the
    service cannot be leaked by a later template change.
    """
    try:
        rows = db.get_agent_executions_summary(agent_name, limit=limit)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: executions read failed for %s: %s", agent_name, e)
        return []
    return [{
        "id": r.get("id"),
        "status": r.get("status"),
        "triggered_by": r.get("triggered_by"),
        "started_at": r.get("started_at"),
        "completed_at": r.get("completed_at"),
        "duration_ms": r.get("duration_ms"),
    } for r in rows]


def reports(agent_name: str, limit: int = 20, offset: int = 0) -> list[dict]:
    """Metadata for the Reports tab (payload fetched separately when expanded).

    Reuses the existing report surface (#918) exactly as the Technical Notes
    ask. Metadata only: a payload is up to 256 KB and belongs on expansion.
    """
    try:
        rows = db.get_reports_for_agent(agent_name, limit=limit, offset=offset)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: reports read failed for %s: %s", agent_name, e)
        return []
    return [{
        "id": r.get("id"),
        "report_type": r.get("report_type"),
        "title": r.get("title"),
        "display_hint": r.get("display_hint"),
        "period_start": r.get("period_start"),
        "period_end": r.get("period_end"),
        "created_at": r.get("created_at"),
    } for r in rows]


def build_page(email: str, agent_name: str, card: Optional[dict],
               window: str = DEFAULT_WINDOW) -> dict:
    """Assemble the page. `card` is the caller's roster entry (identity + what
    it can do), already resolved and access-checked by the caller.

    On the "rating tally" named in AC #3: there is no rating, thumbs or feedback
    mechanism anywhere in Trinity, so it has no data source and is omitted
    rather than invented — a number a user reads as "how well is this agent
    doing" has to come from something real. Recorded in the requirement as the
    one AC bullet not met. (The first-try rate beside it IS real: it comes from
    `retry_count`, see `portal_db.first_try_stats`.)
    """
    card = card or {}
    return {
        "agent_name": agent_name,
        "header": {
            "name": agent_name,
            "description": card.get("description"),
            "avatar_url": card.get("avatar_url"),
            "owner": card.get("owner"),
            "health": _health(agent_name),
            "last_active": _last_active(agent_name),
        },
        # "What it can do" — a projection of the briefing the roster already
        # carries (#138 / ent#380), NOT a second mechanism. ent#178 is the
        # unified exposable-skills config this becomes a view of when it lands.
        "capabilities": card.get("playbooks") or [],
        "stats": _stats(agent_name, window),
        "asks": _asks(agent_name),
        "recent_work": _recent_work(agent_name),
    }


def _last_active(agent_name: str) -> Optional[str]:
    """When this agent last did anything, from its newest execution row."""
    try:
        rows = db.get_agent_executions_summary(agent_name, limit=1)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: last-active read failed for %s: %s", agent_name, e)
        return None
    return rows[0].get("started_at") if rows else None


def report_detail(agent_name: str, report_id: str) -> Optional[dict]:
    """One report's full payload, scoped to the agent whose page is open.

    The agent check is the point: report ids are global, and without it any
    rostered agent's page would read any report in the install. A foreign id
    returns None → the router's 404, identical to a nonexistent one, so this
    cannot be used to test whether a report exists (invariant #8).
    """
    try:
        row = db.get_report(report_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: report read failed for %s: %s", report_id, e)
        return None
    if not row or row.get("agent_name") != agent_name:
        return None
    return {
        "id": row.get("id"),
        "agent_name": row.get("agent_name"),
        "report_type": row.get("report_type"),
        "title": row.get("title"),
        "display_hint": row.get("display_hint"),
        "payload": row.get("payload"),
        "period_start": row.get("period_start"),
        "period_end": row.get("period_end"),
        "created_at": row.get("created_at"),
    }
