# mcp: none — renders dashboard.yaml for the Vue dashboard only; no external consumer
"""
Agent Dashboard routes.

Provides endpoint for fetching agent dashboard configuration with
optional history enrichment and platform metrics (DASH-001).
Uses /api/agent-dashboard prefix to avoid confusion with main dashboard.
"""
import logging
from fastapi import APIRouter, Depends, Query

from models import User
from dependencies import AuthorizedAgentByName, get_current_user
from services.agent_service.dashboard import get_agent_dashboard_logic
from database import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent-dashboard", tags=["agent-dashboard"])


@router.get("/{name}")
async def get_agent_dashboard(
    name: str,
    include_history: bool = Query(
        default=True,
        description="Include historical sparkline data for widgets"
    ),
    history_hours: int = Query(
        default=24,
        ge=1,
        le=168,
        description="Hours of history to include (1-168)"
    ),
    include_platform_metrics: bool = Query(
        default=True,
        description="Include platform-managed metrics section"
    ),
    current_user: User = Depends(get_current_user)
):
    """
    Get agent dashboard configuration.

    Returns dashboard configuration from the agent's dashboard.yaml file,
    enriched with historical data for sparklines and platform metrics.

    Query parameters:
    - include_history: Include sparkline data for metric/progress/status widgets (default: true)
    - history_hours: Hours of history to include, 1-168 (default: 24)
    - include_platform_metrics: Include platform-managed section with tasks/cost/health (default: true)

    Widget types supported:
    - metric: Single numeric value with optional trend and sparkline
    - status: Colored status badge
    - progress: Progress bar (0-100)
    - text: Simple text
    - markdown: Rich text with markdown rendering
    - table: Tabular data
    - list: Bullet or numbered list
    - link: Clickable link or button
    - image: Image display
    - divider: Horizontal separator
    - spacer: Vertical space

    History enrichment adds to trackable widgets:
    - history.values: Array of {t: timestamp, v: value}
    - history.trend: "up", "down", or "stable"
    - history.trend_percent: Percentage change
    - history.min, history.max, history.avg: Statistical values

    Platform metrics section (platform_managed: true):
    - Tasks (24h): Number of executions
    - Success Rate: Percentage of successful tasks
    - Cost (24h): Total API cost
    - Health: Current health status
    - Running: Number of active executions

    Agents can opt out of platform metrics by setting `platform_metrics: false` in dashboard.yaml.
    """
    return await get_agent_dashboard_logic(
        name,
        current_user,
        include_history=include_history,
        history_hours=history_hours,
        include_platform_metrics=include_platform_metrics
    )


# The path parameter is `agent_name`, not `name`, because that is the name
# `get_authorized_agent_by_name` declares its `Path(...)` under — a route that
# spells it `{name}` leaves the dependency with no path parameter to bind and
# answers 422 to EVERY caller, including the owner. The URL is unchanged
# (`/api/agent-dashboard/{agent}/exists`); only the binding name moved.
@router.get("/{agent_name}/exists")
async def check_dashboard_exists(agent_name: AuthorizedAgentByName):
    """
    Lightweight check for what the Dashboard tab would have to show.

    Two flags, one request (ent#479, TD-9): `has_dashboard` (a cached
    `dashboard.yaml`) and `has_declared_metrics` (a `template.yaml metrics:`
    block the registry holds). The tab appears for EITHER — declared metrics
    render as tiles with no `dashboard.yaml` at all, which is the whole point
    of ent#439's sensible default.

    DB-only on both halves — no container call — so it answers for a stopped
    agent and the frontend needs no status gate for the metrics half.

    Gated with the uniform-404 dependency, which it was not before: a bare
    `get_current_user` made this a fleet-wide existence oracle for any logged
    in principal (E-S2). The frontend probe already treats a throw as "no
    dashboard", so closing it changes nothing a legitimate caller sees.
    """
    try:
        has_declared_metrics = bool(db.list_metric_definitions(agent_name))
    except Exception as e:  # noqa: BLE001 — the tab gate is not worth a 500
        logger.warning("[Dashboard] Declared-metric probe failed for %s: %s",
                       agent_name, e)
        has_declared_metrics = False
    return {
        "has_dashboard": db.has_cached_dashboard(agent_name),
        "has_declared_metrics": has_declared_metrics,
    }
