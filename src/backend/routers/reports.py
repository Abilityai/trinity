"""
Agent reports API (#918).

Agents publish structured reports (telemetry / domain reports) via the MCP
``report`` tool, which POSTs here. Reports are persisted, broadcast as a thin
WebSocket trigger, and surfaced on the Agent Detail "Reports" tab and a
fleet-wide Reports view.

Access control mirrors routers/executions.py:
- admin → every report (agent_names=None, no SQL filter)
- non-admin → only accessible agents (owned + shared)

Create is **self-gated** (review/Codex #1): ``AuthorizedAgent`` checks the key
owner can access the path agent, but does NOT stop an agent-scoped key from
reporting as a *sibling* agent the owner also accesses. So we additionally
require an agent-scoped caller's bound ``agent_name`` to equal the path agent
(mirrors heartbeat_service.authorize_heartbeat). User-scoped callers fall back to
the access check.
"""
import json
import logging
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from database import db
from dependencies import (
    get_current_user,
    AuthorizedAgent,
    OwnedAgent,
    is_interactive_principal,
)
from models import (
    FleetReportStats,
    Report,
    ReportCreate,
    ReportSummary,
    REPORT_PAYLOAD_MAX_BYTES,
    REPORT_ROWS_PAGE_DEFAULT,
    REPORT_ROWS_PAGE_MAX,
    User,
)
from services import rate_limiter, report_export, report_service
from services.agent_service.helpers import accessible_agent_names, narrow_to_agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["reports"])


def _hide_audience(row: dict, current_user: User) -> dict:
    """Withhold `addressed_to` from any caller that is not an interactive human.

    ent#365 review. The audience strip shipped in the MCP tool only
    (`stripAudienceFromReports`), but an agent-scoped MCP key is a valid bearer
    token against this API directly - that is how the heartbeat, the #1083
    result callback and the reports WRITE path all authenticate. So an agent
    could `curl` these routes and read `addressed_to` for every agent its owner
    can access, which is strictly wider than the `{self} u permitted` scope the
    MCP layer enforces.

    The PR's own rationale for stripping at the tool was "a tool result is LLM
    context wherever it lands". That argument applies at least as strongly to a
    shell result, since the threat model is a prompt-injected agent and such an
    agent has Bash. Closing it here rather than recording a residual, because
    the alternative leaves code that READS as though the hole is shut.

    The UI is unaffected: it reads over a JWT, which is exactly the allowlisted
    case. The predicate is `dependencies.is_interactive_principal` - shared with
    `reject_non_interactive_principal` so the two cannot drift, and an allowlist
    rather than a denylist because `mcp_api_keys.scope` has no CHECK constraint
    and the next scope to ship would otherwise be admitted silently (#2323).
    """
    if is_interactive_principal(current_user):
        return row
    if "addressed_to" not in row:
        return row
    redacted = dict(row)
    redacted.pop("addressed_to", None)
    return redacted


def _resolve_portal_session(execution_id: str, agent_name: str) -> Optional[str]:
    """The Workspace chat a publishing turn belongs to, or None (ent#365).

    Two gates, in this order: the execution must belong to THIS agent
    (`resolve_and_validate_execution`, the MEM-001 rule — the agent supplies an
    id, never its own identity), and the id must be the turn currently in flight
    for a portal session, which is what the ent#286 reverse marker answers.

    Fail-soft to None everywhere: a report with no chat still lists on the agent
    page, whereas a 5xx here would fail a publish over a card placement. The
    marker is Redis-backed with a TTL sized to the turn, so a report published
    after its own turn ended lands unlinked — correct, since by then the client
    has the reply and the card belongs to the page, not to a closed exchange.
    """
    try:
        from services.idempotency_service import resolve_and_validate_execution
        if resolve_and_validate_execution(execution_id, agent_name) is None:
            return None
        from client_portal import service as portal_service
        return portal_service.get_inflight_session_for_execution(execution_id)
    except Exception as e:  # noqa: BLE001
        # WARNING, not debug (caught in review on #2383). Fail-soft is right —
        # a card placement must never fail a publish — but this is the one
        # function the entire in-chat half of the deliverable depends on. A
        # Redis outage, an import error or a renamed marker key would make
        # every card silently stop appearing while the agent page still lists
        # the reports, so nothing would give anyone a reason to look.
        logger.warning(
            "portal session resolution failed for execution %s (%s) — the "
            "report will publish without an in-chat card",
            execution_id, type(e).__name__,
        )
        return None

_VALID_HOURS = {0, 1, 6, 24, 168, 720}  # 0 = all-time

# Per-agent create rate limit (#918 review I3). Reports can be bursty (an agent
# may publish a few at the end of a run), so the window is generous — its job is
# to cap a runaway/looping agent flooding the table between retention sweeps, not
# to throttle normal use. Shared sliding-window limiter (services/rate_limiter.py,
# #1023); fail-open if Redis is down.
REPORT_RATE_LIMIT = int(os.getenv("REPORT_RATE_LIMIT", "30"))
REPORT_RATE_WINDOW = 60  # seconds


# ============================================================================
# Agent-scoped endpoints  (/api/agents/{name}/reports)
# ============================================================================


def _report_or_404(report_id: str, current_user: User) -> dict:
    """Fetch a report and gate it, or 404.

    ONE gate for all three read routes (detail / rows / export). It answers
    **404, never 403**, for a missing report AND for one the caller cannot
    reach: a 403 would confirm the id exists, turning any of these routes into
    an existence oracle for another tenant's reports.

    Extracted per review of #1838: the three routes each carried a byte-identical
    copy of this check, which is exactly the duplication Invariant #8's static
    guard (`test_1310_auth_wiring`) proxies for — three copies are three chances
    for one to drift into a 403, or to lose the access half entirely. The
    allowlist entry is now this single helper rather than one per route.
    """
    report = db.get_report(report_id)
    if not report or not db.can_user_access_agent(current_user.username, report["agent_name"]):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return report


@router.post("/agents/{name}/reports", response_model=Report, status_code=201)
async def create_report(
    data: ReportCreate,
    name: AuthorizedAgent,
    # Defaulted so a direct in-process call (tests, any future internal caller)
    # keeps working; FastAPI injects it regardless of the default, and the
    # header guard below already treats absence as "no hint available".
    #
    # Deliberately NOT `Optional[Request]` (raised in #1838 review, verified
    # wrong): FastAPI special-cases the BARE `Request` annotation as an ASGI
    # injection. Wrapping it in Optional loses that, so FastAPI tries to build a
    # Pydantic field for it and the module fails to import:
    #   FastAPIError: Invalid args for response field! ... typing.Optional[
    #   starlette.requests.Request] is a valid Pydantic field type
    # The annotation is a framework contract here, not a nullability claim.
    request: Request = None,
    current_user: User = Depends(get_current_user),
):
    """Publish a report for an agent (called by the agent via MCP).

    Self-gated: an agent-scoped key may only report as itself.
    """
    if current_user.agent_name and current_user.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent-scoped key may only report as itself",
        )

    # Cap report volume per agent so a runaway agent can't flood the table
    # between retention sweeps (review I3). Fail-open if Redis is down.
    rate_limiter.enforce(
        f"agent_report:{name}",
        REPORT_RATE_LIMIT,
        REPORT_RATE_WINDOW,
        detail="Report rate limit exceeded for this agent.",
    )

    # Two-stage size guard (#1537). The declared Content-Length is checked first
    # so an oversized body is refused on the cheap header rather than after
    # serializing the parsed payload back to JSON — at a 5 MiB ceiling that
    # round-trip is the expensive part. It is a HINT, not the enforcement: a
    # missing or lying header falls through to the exact check below, which is
    # what actually bounds what reaches the DB and every later read.
    #
    # Honest limit: Starlette has already buffered the body by the time this
    # handler runs, so this bounds STORAGE and response size, not peak memory.
    # A true streaming guard needs the body off the typed-model path (the
    # webhooks.py pattern) — worth doing only if reports ever move to the
    # multi-megabyte norm this ceiling merely permits.
    declared = request.headers.get("content-length") if request else None
    if declared:
        try:
            if int(declared) > REPORT_PAYLOAD_MAX_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"payload exceeds {REPORT_PAYLOAD_MAX_BYTES} bytes",
                )
        except ValueError:
            pass  # unparseable header — the exact check below still applies

    # Bound the payload before it hits the DB / every list response (review A2).
    if len(json.dumps(data.payload).encode("utf-8")) > REPORT_PAYLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"payload exceeds {REPORT_PAYLOAD_MAX_BYTES} bytes",
        )

    # ent#365 — the audience, checked against the agent's OWN roster. An agent
    # naming an address it does not already talk to is refused by name rather
    # than silently stored: the column decides whose Workspace this appears in,
    # so an unchecked value would let an agent post its output into a stranger's
    # surface. `email_has_agent_access` is the same predicate the #848 inline
    # auth path gates on — but that predicate is BROADER than the Workspace read
    # gate, and the difference is a publish that nobody can read (review
    # finding): `email_has_agent_access` returns True for any user whose role is
    # `admin`, regardless of sharing, while `agent_on_roster` is `agent_sharing`
    # ∪ owned. So a report addressed to a platform admin who neither owns nor is
    # shared this agent stored happily and 404'd in that admin's Workspace,
    # falsifying the invariant stated one line above.
    #
    # The publish therefore gates on the SAME predicate the reader uses. Import
    # is local: `client_portal` is a sibling package and a module-level import
    # here would couple the reports router to it at load time.
    audience = data.audience_email
    if audience:
        try:
            from client_portal.service import agent_on_roster
            # `include_owned=False`: an owner reads their agent's reports on the
            # operator surface, and addressing one to themselves is not what the
            # audience column is for.
            reachable = agent_on_roster(name, audience, include_owned=False)
        except Exception as e:  # noqa: BLE001 — an unreadable roster must not publish
            logger.warning("report audience check failed for %s: %s", name, e)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not verify the report audience — try again.",
            )
        if not reachable:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "audience_email is not a client of this agent — share the "
                    "agent with that address first, or omit it to publish an "
                    "operator-only report."
                ),
            )

    # Which Workspace chat this belongs in, resolved from the publishing TURN.
    # Never read from the request: the agent supplies an execution id, the
    # backend decides what conversation that is (the MEM-001 rule). Absent,
    # unresolvable, or a non-portal turn ⇒ NULL, and the report simply lists on
    # the agent page without a chat card.
    portal_session_id = None
    if audience and data.execution_id:
        portal_session_id = _resolve_portal_session(data.execution_id, name)

    report = await report_service.create_report(
        agent_name=name,
        user_id=current_user.id,
        report_type=data.report_type,
        title=data.title,
        payload=data.payload,
        display_hint=data.display_hint,
        schema_version=data.schema_version,
        period_start=data.period_start,
        period_end=data.period_end,
        addressed_to_email=audience,
        portal_session_id=portal_session_id,
    )
    # The create dict now carries two fields the response model does not declare.
    # Pydantic v2 ignores unknown keys by default, so this filter is a belt, not
    # the mechanism.
    #
    # Review finding: the comment used to say it "keeps the audience out of the
    # response shape", which stopped being true the moment `ReportSummary` gained
    # `addressed_to` — `Report` extends it, so the field IS in `model_fields`, and
    # the filter was instead silently dropping it because `db.create_report`
    # returns the column under its DB name (`addressed_to_email`). So the same
    # model meant two different things depending on the route: populated on
    # `GET /reports/{id}` (via `_mapping_to_report`), always null here. Two
    # intentions contradicting each other, with the MCP tool papering over it by
    # echoing back the caller's own argument.
    #
    # Mapped explicitly, so the response says what the row says.
    projected = {k: v for k, v in report.items() if k in Report.model_fields}
    if "addressed_to" not in projected and "addressed_to_email" in report:
        projected["addressed_to"] = report["addressed_to_email"]
    return Report(**projected)


@router.get("/agents/{name}/reports", response_model=List[ReportSummary])
async def list_agent_reports(
    name: AuthorizedAgent,
    report_type: Optional[str] = Query(None),
    hours: int = Query(168, description="Time window in hours; 0 = all-time"),
    search: Optional[str] = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """List one agent's reports (metadata only, newest first).

    `hours`/`search` (#1539) bring this route to parity with the fleet list.
    They were absent, which made the per-agent Reports tab a flat unfilterable
    list and silently dropped both filters for any caller that scoped to one
    agent. `hours` is whitelist-validated exactly like the fleet route — an
    unlisted value falls back to the 7-day default rather than erroring, so an
    old client keeps working.
    """
    effective_hours = hours if hours in _VALID_HOURS else 168
    rows = db.get_reports_for_agent(
        name,
        report_type=report_type,
        hours=effective_hours or None,
        search=search,
        limit=limit,
        offset=offset,
    )
    return [ReportSummary(**_hide_audience(r, current_user)) for r in rows]


@router.delete("/agents/{name}/reports/{report_id}", status_code=204)
async def delete_report(name: OwnedAgent, report_id: str):
    """Delete a report (owner/admin). Scoped by (agent_name, id) → 404 on mismatch."""
    if not db.delete_report(name, report_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return None


# ============================================================================
# Fleet-wide endpoints  (/api/reports)
# `/reports/stats` is declared before `/reports/{report_id}` (Invariant #4).
# ============================================================================

@router.get("/reports/stats", response_model=FleetReportStats)
async def get_fleet_report_stats(
    report_type: Optional[str] = Query(None),
    hours: int = Query(168, description="Time window in hours; 0 = all-time"),
    agent: Optional[str] = Query(None, description="Filter to a single agent"),
    current_user: User = Depends(get_current_user),
):
    """Aggregate stat-card data for the fleet Reports view."""
    agent_names = narrow_to_agent(accessible_agent_names(current_user), agent)
    effective_hours = hours if hours in _VALID_HOURS else 168
    stats = db.get_fleet_report_stats(
        agent_names, report_type=report_type, hours=effective_hours or None
    )
    return FleetReportStats(**stats)


@router.get("/reports", response_model=List[ReportSummary])
async def list_fleet_reports(
    report_type: Optional[str] = Query(None),
    hours: int = Query(168, description="Time window in hours; 0 = all-time"),
    search: Optional[str] = Query(None, max_length=200),
    agent: Optional[str] = Query(None, description="Filter to a single agent"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """List reports across all accessible agents (metadata only)."""
    agent_names = narrow_to_agent(accessible_agent_names(current_user), agent)
    effective_hours = hours if hours in _VALID_HOURS else 168
    rows = db.get_fleet_reports(
        agent_names,
        report_type=report_type,
        hours=effective_hours or None,
        search=search,
        limit=limit,
        offset=offset,
    )
    return [ReportSummary(**_hide_audience(r, current_user)) for r in rows]


@router.get("/reports/{report_id}/export")
async def export_report(
    report_id: str,
    format: str = Query("xlsx", pattern="^(xlsx|pdf)$"),
    current_user: User = Depends(get_current_user),
):
    """Download a report as a spreadsheet or a PDF (#1536).

    Access is the same gate as the detail route, including its 404-not-403 for a
    report the caller cannot reach — an export URL must not become the oracle the
    detail route refuses to be.

    Shape mismatches degrade rather than fail: a `kpi` payload exports as a
    two-column sheet, an unrecognized one as pretty-printed JSON. The only 4xx
    here is an unknown `format`.

    Missing libraries answer **503, not 500**: they are pinned in the backend
    image, but an instance that upgrades code without rebuilding (#1814) would
    otherwise see an opaque crash instead of "rebuild the image".
    """
    report = _report_or_404(report_id, current_user)

    title = report.get("title") or "report"
    try:
        if format == "xlsx":
            content = report_export.build_xlsx(report.get("payload"), report.get("display_hint"), title)
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            content = report_export.build_pdf(report.get("payload"), report.get("display_hint"), title)
            media = "application/pdf"
    except report_export.ExportUnavailable as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Report export is unavailable on this instance ({e}). "
                "Rebuild the backend image so the export dependencies are installed."
            ),
        )

    filename = _export_filename(title, report_id, format)
    return Response(
        content=content,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # The report body is agent-authored; keep browsers from sniffing it
            # into something executable, matching the FILES-001 download route.
            "X-Content-Type-Options": "nosniff",
        },
    )


def _export_filename(title: str, report_id: str, fmt: str) -> str:
    """A safe, recognizable filename. The title is agent-authored, so anything
    that could break out of the quoted Content-Disposition value — quotes,
    newlines, separators — is stripped rather than escaped."""
    safe = "".join(ch if (ch.isalnum() or ch in " -_") else "-" for ch in (title or ""))
    safe = "-".join(safe.split()).strip("-")[:60] or "report"
    return f"{safe}-{report_id[:8]}.{fmt}"


@router.get("/reports/{report_id}/rows")
async def get_report_rows(
    report_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(REPORT_ROWS_PAGE_DEFAULT, ge=1, le=REPORT_ROWS_PAGE_MAX),
    current_user: User = Depends(get_current_user),
):
    """A PAGE of a tabular report's rows, so a large table is never shipped whole
    (#1537).

    `GET /reports/{id}` returns the entire payload — fine at 201 bytes (the fleet
    average when this was measured), wrong at the 5 MiB the cap now permits. This
    gives the renderer a window instead: columns once, then `limit` rows from
    `offset`, plus the true `total` so the UI can show "100 of 12,431".

    Only `table`-shaped payloads (`{columns, rows}`) paginate — every other
    display_hint is a bounded document (a KPI tile set, a markdown body) with no
    row axis to slice, and answering 400 for those is clearer than inventing one.

    Honest limit: the row slice happens in Python after the whole blob is read
    from the column, so this bounds the RESPONSE, not the read. Slicing in the
    database needs the rows off-row — deliberately not built until real payloads
    justify the migration (see REPORT_PAYLOAD_MAX_BYTES).
    """
    report = _report_or_404(report_id, current_user)

    payload = report.get("payload")
    columns = payload.get("columns") if isinstance(payload, dict) else None
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not isinstance(columns, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Report payload is not tabular ({columns, rows}); fetch it whole via GET /api/reports/{id}",
        )

    return {
        "report_id": report_id,
        "columns": columns,
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "rows": rows[offset : offset + limit],
    }


@router.get("/reports/{report_id}", response_model=Report)
async def get_report(report_id: str, current_user: User = Depends(get_current_user)):
    """Full report incl. payload. 404 (not 403) on no-access to avoid id leak."""
    return Report(**_hide_audience(_report_or_404(report_id, current_user), current_user))
