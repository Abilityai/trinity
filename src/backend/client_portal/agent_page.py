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

* `recent_work` is projected down to status/trigger/time/duration, plus the
  **schedule name** (#2161). The underlying accessor also returns `message`,
  `cost`, `model_used` and `source_user_email` — another user's prompt, and two
  things AC #7 excludes. The schedule name is the one string that deliberately
  does cross, because rows reading "Scheduled run" nine times tell the reader
  nothing; it is a short label, never the schedule's `message` (which is a
  prompt and is exactly what this page must not show).

  It is **not** guaranteed to be operator-authored, and calling it that would be
  the comfortable mistake: `POST /api/agents/{name}/schedules` is
  `AuthorizedAgent` and the MCP `create_agent_schedule` tool exists, so an
  agent-scoped key — hence a prompt-injected agent — can write the text that
  renders on a client's page. It is therefore treated as untrusted content and
  bounded (`MAX_SCHEDULE_NAME_CHARS`); Vue escapes it on render. The same is
  already true of `asks` (`title`/`question` are agent-authored), so this is the
  established boundary rather than a new one — but it is the reason the name is
  capped and the prompt is never loaded at all.
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
from models import REPORT_ROWS_PAGE_MAX

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

# Executions the platform creates outside a cron schedule carry this sentinel
# instead of a real schedule id (reminders, manual chat turns), so it can never
# resolve to a name and is not worth a query.
NO_SCHEDULE_ID = "__manual__"

# `ScheduleCreate.name` carries no length bound, so the row label is capped
# here rather than trusted. Long enough to tell schedules apart, short enough
# that one cannot take over the list.
MAX_SCHEDULE_NAME_CHARS = 80


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
            "success_rate": None, "timeline": [], "by_type": [], "buckets": [],
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
        # #2161: the accessor already computes the canonical stack order
        # (`_BUCKET_ORDER` filtered to what is present). Forwarding it keeps that
        # order in ONE place — deriving it client-side from `by_type` would be
        # equivalent today and free to diverge tomorrow.
        "buckets": a.get("buckets", []),
        # AC #3's "first-try rate". Distinct from success_rate above, which
        # counts a retried-then-succeeded execution as a success.
        "first_try": portal_db.first_try_stats(agent_name, hours),
        "unavailable": False,
    }


def _asks(agent_name: str, viewer_email: str) -> list[dict]:
    """What this agent is waiting on THIS VIEWER for.

    Read-only here. Answering an approval writes to the operator queue, which is
    an operator surface with its own auth; the page offers "reply in chat"
    instead, so neither audience lands on a control that does nothing.

    `viewer_email` is not decoration (ent#428). One agent is routinely shared
    with several clients, and this block used to be scoped by `agent_name`
    alone — so every co-shared client read every other client's pending ask,
    title and question verbatim. `context` was already withheld here as a known
    leak surface, but `title`/`question`/`options` are agent-authored free text
    and are exactly where an ask addressed to someone else says something not
    meant for this reader. `addressed_to_email` exists to decide *whose* surface
    an ask appears on (ent#364); this reader predates it and was never taught.

    Unaddressed operator asks (`addressed_to_email IS NULL`) are excluded by the
    same condition, and that is the intended narrowing rather than a side
    effect: this is a client-facing page, an operator ask is agent-authored text
    written for the operator, and a client cannot act on one from here anyway.
    Operators keep the full queue in Operations, and this matches what the
    Workspace's other ask surfaces already show the same person.
    """
    try:
        items = db.list_operator_queue_items(
            status="pending", agent_name=agent_name,
            addressed_to_email=viewer_email, limit=MAX_ASKS,
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


def _schedule_names(agent_name: str, rows: list[dict]) -> dict:
    """`schedule_id` → schedule NAME, for the rows that actually carry one (#2161).

    One query for the whole page, never `db.get_schedule` per row: that is an N+1,
    and it would also throw away the agent scoping this shape gets for free. The
    map is built from *this* agent's own schedules, so a foreign or stale id
    simply misses and the row falls back to its trigger label.

    Only `id → name`, and the accessor is a **projected SELECT** rather than
    `list_agent_schedules`, which returns whole `Schedule` models carrying
    `message` and `validation_prompt`. Reading those and then choosing not to
    return them would make this module's stated principle — a field that never
    leaves the service cannot be leaked by a later edit — a review invariant.
    Not loading them makes it structural, and it is one edit (`{s.id: s}`, or a
    `description` for a subtitle) away from mattering.

    The name is **truncated**: `ScheduleCreate.name` has no length bound, and
    the writer is not necessarily a human (see the note in the module docstring),
    so an unbounded string reaches a client page otherwise.

    Fails soft: a schedules read that raises costs the labels, never the rows.
    """
    wanted = {
        r.get("schedule_id") for r in rows
        if r.get("schedule_id") and r.get("schedule_id") != NO_SCHEDULE_ID
    }
    if not wanted:
        return {}
    try:
        # Already excludes soft-deleted rows (#834) — a deleted schedule's
        # historical executions read as a plain trigger label, which is honest.
        names = db.get_agent_schedule_names(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: schedule names failed for %s: %s", agent_name, e)
        return {}
    return {
        sid: name[:MAX_SCHEDULE_NAME_CHARS]
        for sid, name in names.items() if sid in wanted and name
    }


def _recent_work(agent_name: str, limit: int = MAX_RECENT_WORK) -> list[dict]:
    """What the agent has been doing — shape, plus the schedule's name.

    The accessor returns `message`, `cost`, `model_used` and `source_user_email`
    among others. A Workspace viewer may be an external client, so the prompt
    text of somebody else's task, the spend, and the model are all projected
    away here rather than filtered in the UI: a field that never leaves the
    service cannot be leaked by a later template change.

    `schedule_name` (#2161) is the deliberate exception, because without it every
    scheduled row rendered the same three words. It attaches to any row whose id
    resolves — not only `triggered_by == "schedule"` — since a webhook that fires
    a schedule *is* running that schedule, and naming it is the point.
    """
    try:
        rows = db.get_agent_executions_summary(agent_name, limit=limit)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: executions read failed for %s: %s", agent_name, e)
        return []
    names = _schedule_names(agent_name, rows)
    return [{
        "id": r.get("id"),
        "status": r.get("status"),
        "triggered_by": r.get("triggered_by"),
        "started_at": r.get("started_at"),
        "completed_at": r.get("completed_at"),
        "duration_ms": r.get("duration_ms"),
        "schedule_name": names.get(r.get("schedule_id")),
    } for r in rows]


def reports(agent_name: str, client_email: str, *, limit: int = 20, offset: int = 0,
            portal_session_id: str | None = None) -> list[dict]:
    """Deliverables **addressed to this person** by this agent (ent#365).

    Reuses the existing report surface (#918) exactly as the Technical Notes
    ask. Metadata only: a payload is up to 5 MiB (`REPORT_PAYLOAD_MAX_BYTES`,
    raised from 256 KB in #1537) and belongs on expansion.

    **Behaviour change, deliberate.** This used to call
    `db.get_reports_for_agent`, which is the OPERATOR question — everything the
    agent ever published — so every rostered client of an agent saw every report
    it had produced, including ones produced for a different client, rendered
    from agent-authored JSON on a client-facing surface. The Workspace asks a
    different question, and ent#365 AC #4 states it: a user sees their own, not
    the fleet. Unaddressed reports (`addressed_to_email IS NULL`) are
    operator-only and no longer appear here at all — which is AC #1, and is why
    an install whose agents have not adopted `audience_email` yet will show an
    empty Reports tab until they do.

    The scope is the caller's identity for BOTH principal kinds. A platform user
    on this surface is using the client surface and sees what was addressed to
    them; everything else stays on the operator Reports tab, which is where the
    fleet view belongs.

    `portal_session_id` narrows further to one chat, which is what the inline
    deliverable cards read.
    """
    try:
        rows = db.get_reports_for_client(
            agent_name, client_email, portal_session_id=portal_session_id,
            limit=limit, offset=offset,
        )
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
            # #2196: a projection of the card the caller already resolved, NOT a
            # second Docker read — and `or "unknown"`, never a bare `.get`.
            # `card` is documented-reachable as None (the agent vanished between
            # the roster read and this one), which `card = card or {}` above
            # turns into an explicit None here; a Literal-with-default REJECTS
            # an explicit None, so the bare form would 500 the one page ent#360
            # built to always render.
            "availability": card.get("availability") or "unknown",
            "last_active": _last_active(agent_name),
        },
        # "What it can do" — a projection of the briefing the roster already
        # carries (#138 / ent#380), NOT a second mechanism. ent#178 is the
        # unified exposable-skills config this becomes a view of when it lands.
        "capabilities": card.get("playbooks") or [],
        "stats": _stats(agent_name, window),
        "asks": _asks(agent_name, email),
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


def _window_rows(payload, offset: int, limit: int):
    """Slice a tabular payload's `rows`, or leave it alone (#2162).

    Returns `(payload, row_meta)` — `row_meta` is None whenever no window was
    applied, which is how the caller (and, downstream, the renderer's footer)
    tells a windowed table from a whole document.

    **The server decides tabularity, from the real payload.** `display_hint` is
    agent-authored and can disagree with what was actually filed, so a client
    that predicted the shape would need a 400 and a recovery re-fetch for the
    disagreement. Answering "here it is whole, and no, that wasn't a table"
    removes that branch: one request, always. Every other display_hint is a
    bounded document (a KPI tile set, a markdown body) with no row axis to
    slice, so there is nothing to answer 400 about.

    Subtractive on `rows` only: sibling keys are returned exactly as filed, the
    same as the unwindowed path. The copy is shallow so the source row is never
    truncated in place.
    """
    if not isinstance(payload, dict):
        return payload, None
    columns = payload.get("columns")
    rows = payload.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return payload, None

    # A negative offset would silently serve rows counted from the END under an
    # honest-looking total; an unbounded limit would defeat the point of the
    # window. The route validates both (422), but the clamp is what actually
    # bounds the response and must not depend on one caller doing so.
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), REPORT_ROWS_PAGE_MAX))

    windowed = dict(payload)
    windowed["rows"] = rows[offset:offset + limit]
    # `total` is the TRUE row count, not the window — it is what "Showing 100 of
    # 12,431" reads, and a windowed total would hide the rest behind a footer
    # that never appears.
    return windowed, {"total": len(rows), "offset": offset, "limit": limit}


def report_detail(agent_name: str, report_id: str, *,
                  client_email: str,
                  rows_offset: int = 0,
                  rows_limit: Optional[int] = None) -> Optional[dict]:
    """One report's full payload, scoped to the agent whose page is open.

    The agent check is the point: report ids are global, and without it any
    rostered agent's page would read any report in the install. A foreign id
    returns None → the router's 404, identical to a nonexistent one, so this
    cannot be used to test whether a report exists (invariant #8).

    `rows_limit` (#2162) windows a tabular payload's rows, so a large table is
    never shipped whole to a client — the #1537 pattern, reached here through
    two optional params rather than a second route. The operator row reader is
    `Depends(get_current_user)`, which a portal principal (a verified email with
    no `users` row) structurally cannot satisfy; cloning it on a client-facing
    prefix would mean a second hand-written gate beside the one above, and that
    is how a 404-uniformity contract drifts. Absent `rows_limit`, this returns
    byte-for-byte what it returned before the parameter existed.

    Honest limit, same as the operator route's: the slice happens in Python
    after the whole blob is read out of the column, so this bounds the RESPONSE,
    not the read — and therefore paging MULTIPLIES reads. That is why the route
    rate-limits (a client can loop it; an operator on a JWT is a different risk).
    """
    try:
        # ent#365: the audience gate first, then the agent gate. Same shape as
        # `_asks`' ent#428 fix on the sibling surface — a report id is global,
        # and a co-shared client reading another client's deliverable is the
        # same defect as reading their ask, over a bigger blob.
        row = db.get_report_for_client(report_id, client_email)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: report read failed for %s: %s", report_id, e)
        return None
    if not row or row.get("agent_name") != agent_name:
        return None

    payload = row.get("payload")
    row_meta = None
    if rows_limit is not None:
        payload, row_meta = _window_rows(payload, rows_offset, rows_limit)

    detail = {
        "id": row.get("id"),
        "agent_name": row.get("agent_name"),
        "report_type": row.get("report_type"),
        "title": row.get("title"),
        "display_hint": row.get("display_hint"),
        "payload": payload,
        "period_start": row.get("period_start"),
        "period_end": row.get("period_end"),
        "created_at": row.get("created_at"),
    }
    # Present ONLY when a window was actually applied: the client keys "is this
    # paged?" off its presence, so an always-present key with null fields would
    # make every bounded document render a Load-more footer it can never satisfy.
    if row_meta is not None:
        detail["row_meta"] = row_meta
    return detail
