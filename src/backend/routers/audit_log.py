"""
Platform Audit Log API (SEC-001 / Issue #20).

Admin-only query interface over the platform `audit_log` table.
Phases 1–2b: schema, service, write integrations.
Phase 3: MCP tool call audit.
Phase 4: hash chain verification, CSV/JSON export.

Mounted at `/api/audit-log` rather than `/api/audit` to coexist with the
existing Process Engine audit router (`routers/audit.py`) without breaking
URL contracts. A unified surface can be added later.
"""

import csv
import io
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from database import db
from dependencies import require_admin
from models import User
from services.platform_audit_service import platform_audit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audit-log", tags=["audit-log"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AuditLogEntry(BaseModel):
    """Single audit log row as returned to API clients."""

    id: int
    event_id: str
    event_type: str
    event_action: str
    actor_type: str
    actor_id: Optional[str] = None
    actor_email: Optional[str] = None
    actor_ip: Optional[str] = None
    mcp_key_id: Optional[str] = None
    mcp_key_name: Optional[str] = None
    mcp_scope: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    timestamp: str
    details: Optional[dict] = None
    request_id: Optional[str] = None
    source: str
    endpoint: Optional[str] = None
    previous_hash: Optional[str] = None
    entry_hash: Optional[str] = None
    created_at: Optional[str] = None


class AuditLogListResponse(BaseModel):
    """Paginated list response."""

    entries: List[AuditLogEntry]
    total: int
    limit: int
    offset: int


class AuditLogStatsResponse(BaseModel):
    """Aggregate counts."""

    total: int
    by_event_type: dict = Field(default_factory=dict)
    by_actor_type: dict = Field(default_factory=dict)


class AuditHeatmapCell(BaseModel):
    """Single populated bucket in the 7×24 dow×hour heatmap."""

    dow: int = Field(..., ge=0, le=6, description="Weekday (0=Sunday)")
    hour: int = Field(..., ge=0, le=23, description="Hour 0–23 UTC")
    count: int = Field(..., ge=0)


class AuditHeatmapResponse(BaseModel):
    """Sparse 7×24 dow×hour heatmap. Zero-count cells omitted."""

    cells: List[AuditHeatmapCell]
    total: int
    max_count: int


class AuditCalendarDay(BaseModel):
    """Single populated day in the calendar heatmap."""

    date: str = Field(..., description="UTC date, ISO 'YYYY-MM-DD'")
    count: int = Field(..., ge=0)


class AuditCalendarResponse(BaseModel):
    """Sparse per-day calendar heatmap (GitHub-style). Quiet days omitted."""

    days: List[AuditCalendarDay]
    total: int
    max_count: int


# ---------------------------------------------------------------------------
# Endpoints — all admin-only
# ---------------------------------------------------------------------------


@router.get("/stats", response_model=AuditLogStatsResponse)
async def audit_log_stats(
    start_time: Optional[str] = Query(None, description="ISO 8601 UTC inclusive lower bound"),
    end_time: Optional[str] = Query(None, description="ISO 8601 UTC inclusive upper bound"),
    _admin: User = Depends(require_admin),
):
    """Aggregate counts by event_type and actor_type for the time window."""
    stats = db.get_audit_stats(start_time=start_time, end_time=end_time)
    return AuditLogStatsResponse(**stats)


@router.get("/heatmap", response_model=AuditHeatmapResponse)
async def audit_log_heatmap(
    start_time: Optional[str] = Query(None, description="ISO 8601 UTC inclusive lower bound"),
    end_time: Optional[str] = Query(None, description="ISO 8601 UTC inclusive upper bound"),
    event_type: Optional[str] = Query(None, description="Optional event_type filter"),
    actor_type: Optional[str] = Query(None, description="Optional actor_type filter"),
    _admin: User = Depends(require_admin),
):
    """Day-of-week × hour-of-day activity heatmap for the time window (#941 v3).

    Buckets use SQLite ``strftime`` over the stored UTC timestamp — no
    timezone shift. Sparse payload (zero-count cells omitted).
    """
    result = db.get_audit_heatmap(
        start_time=start_time,
        end_time=end_time,
        event_type=event_type,
        actor_type=actor_type,
    )
    return AuditHeatmapResponse(**result)


@router.get("/calendar", response_model=AuditCalendarResponse)
async def audit_log_calendar(
    start_time: Optional[str] = Query(None, description="ISO 8601 UTC inclusive lower bound"),
    end_time: Optional[str] = Query(None, description="ISO 8601 UTC inclusive upper bound"),
    event_type: Optional[str] = Query(None, description="Optional event_type filter"),
    actor_type: Optional[str] = Query(None, description="Optional actor_type filter"),
    _admin: User = Depends(require_admin),
):
    """Per-day calendar heatmap (GitHub-style) for the time window (#941 v3.1).

    Complements the dow×hour heatmap: this view shows *when in calendar
    time* activity happened (which days were heavy), the dow×hour view
    shows the *recurring weekly pattern*. Sparse payload — quiet days
    omitted; the frontend lays them onto a dense week × dow grid.
    """
    result = db.get_audit_calendar(
        start_time=start_time,
        end_time=end_time,
        event_type=event_type,
        actor_type=actor_type,
    )
    return AuditCalendarResponse(**result)


# ---------------------------------------------------------------------------
# Phase 4: Hash chain verification
# ---------------------------------------------------------------------------


class AuditVerifyResponse(BaseModel):
    """Hash chain verification result."""

    valid: bool
    checked: int
    first_invalid_id: Optional[int] = None


@router.post("/verify", response_model=AuditVerifyResponse)
async def verify_audit_integrity(
    start_id: int = Query(..., ge=1, description="First row ID to verify"),
    end_id: int = Query(..., ge=1, description="Last row ID to verify (inclusive)"),
    _admin: User = Depends(require_admin),
):
    """Verify hash chain integrity for a range of audit entries.

    Returns whether the chain is intact. Entries without hashes (written
    before hash chain was enabled) are skipped.
    """
    if end_id < start_id:
        raise HTTPException(status_code=400, detail="end_id must be >= start_id")
    result = await platform_audit_service.verify_chain(start_id, end_id)
    return AuditVerifyResponse(**result)


@router.post("/hash-chain/enable")
async def enable_hash_chain(
    enabled: bool = Query(True, description="Enable or disable hash chain"),
    _admin: User = Depends(require_admin),
):
    """Enable or disable hash chain computation for new audit entries."""
    platform_audit_service.enable_hash_chain(enabled)
    return {"hash_chain_enabled": enabled}


# ---------------------------------------------------------------------------
# Phase 4: Export
# ---------------------------------------------------------------------------


@router.get("/export")
async def export_audit_log(
    start_time: str = Query(..., description="ISO 8601 UTC start (inclusive)"),
    end_time: str = Query(..., description="ISO 8601 UTC end (inclusive)"),
    format: str = Query("json", description="Export format: json or csv"),
    _admin: User = Depends(require_admin),
):
    """Export audit log entries for a time range as JSON array or CSV download."""
    if format not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="format must be 'json' or 'csv'")

    entries = db.get_audit_entries(
        start_time=start_time,
        end_time=end_time,
        limit=100_000,
        offset=0,
    )

    if format == "csv":
        if not entries:
            return StreamingResponse(
                iter(["No entries found\n"]),
                media_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
            )

        output = io.StringIO()
        fieldnames = list(entries[0].keys())
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for entry in entries:
            # Flatten dict fields for CSV
            row = {}
            for k, v in entry.items():
                row[k] = str(v) if isinstance(v, (dict, list)) else v
            writer.writerow(row)

        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
        )

    # JSON format
    return {"entries": entries, "count": len(entries), "format": "json"}


# ---------------------------------------------------------------------------
# List + detail endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=AuditLogListResponse)
async def list_audit_log(
    event_type: Optional[str] = Query(None, description="Filter by event_type (e.g. agent_lifecycle)"),
    actor_type: Optional[str] = Query(None, description="Filter by actor_type (user/agent/mcp_client/system)"),
    actor_id: Optional[str] = Query(None, description="Filter by actor_id (user.id or agent_name)"),
    target_type: Optional[str] = Query(None, description="Filter by target_type"),
    target_id: Optional[str] = Query(None, description="Filter by target_id"),
    source: Optional[str] = Query(None, description="Filter by source (api/mcp/scheduler/system)"),
    start_time: Optional[str] = Query(None, description="ISO 8601 UTC inclusive lower bound"),
    end_time: Optional[str] = Query(None, description="ISO 8601 UTC inclusive upper bound"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _admin: User = Depends(require_admin),
):
    """List audit entries newest-first with optional filters and pagination."""
    filters = {
        "event_type": event_type,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "target_type": target_type,
        "target_id": target_id,
        "source": source,
        "start_time": start_time,
        "end_time": end_time,
    }
    entries = db.get_audit_entries(limit=limit, offset=offset, **filters)
    total = db.count_audit_entries(**filters)
    return AuditLogListResponse(
        entries=[AuditLogEntry(**e) for e in entries],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Distinct-value endpoints (#941) — populate dashboard filter dropdowns
# without hardcoding the enum on the frontend. MUST stay above the
# `/{event_id}` catch-all (invariant #4: static routes before parametrised).
# ---------------------------------------------------------------------------


@router.get("/distinct/event-types", response_model=List[str])
async def list_distinct_event_types(
    _admin: User = Depends(require_admin),
):
    """Return sorted unique event_type values present in the audit log."""
    return db.get_distinct_event_types()


@router.get("/distinct/actor-types", response_model=List[str])
async def list_distinct_actor_types(
    _admin: User = Depends(require_admin),
):
    """Return sorted unique actor_type values present in the audit log."""
    return db.get_distinct_actor_types()


@router.get("/{event_id}", response_model=AuditLogEntry)
async def get_audit_log_entry(
    event_id: str,
    _admin: User = Depends(require_admin),
):
    """Look up a single audit entry by its UUID event_id."""
    entry = db.get_audit_entry(event_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Audit log entry not found")
    return AuditLogEntry(**entry)
