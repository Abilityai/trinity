"""
Agent Service Dashboard - Dashboard configuration operations.

Handles fetching dashboard configuration from agents, enriching with history,
and injecting platform metrics (DASH-001).
"""
import copy
import logging
from typing import Optional, Dict

import httpx
from fastapi import HTTPException

from models import User
from database import db
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container
from services.docker_utils import container_reload

logger = logging.getLogger(__name__)


def _build_platform_metrics_section(agent_name: str, hours: int = 24) -> dict:
    """Build the platform metrics section with execution and health stats.

    Args:
        agent_name: Name of the agent
        hours: Time window for stats

    Returns:
        Section dict ready to append to dashboard config
    """
    # Get execution stats
    exec_stats = db.get_agent_execution_stats(agent_name, hours)

    # Get health stats
    health = db.get_latest_health_check(agent_name, "aggregate")

    widgets = []

    # Tasks metric
    widgets.append({
        "type": "metric",
        "id": "__platform_tasks",
        "label": f"Tasks ({hours}h)",
        "value": exec_stats["task_count"],
        "platform_source": "executions.count"
    })

    # Success rate metric
    if exec_stats["task_count"] > 0:
        success_rate = exec_stats["success_rate"]
        widgets.append({
            "type": "metric",
            "id": "__platform_success_rate",
            "label": "Success Rate",
            "value": f"{success_rate}%",
            "color": "green" if success_rate >= 90 else ("yellow" if success_rate >= 70 else "red"),
            "platform_source": "executions.success_rate"
        })

    # Cost metric (only if there was cost)
    if exec_stats["total_cost"] > 0:
        widgets.append({
            "type": "metric",
            "id": "__platform_cost",
            "label": f"Cost ({hours}h)",
            "value": f"${exec_stats['total_cost']:.2f}",
            "platform_source": "executions.cost"
        })

    # Health status
    if health:
        # health is a dict from get_latest_health_check(), access via key not attribute
        health_status = health.get("status", "unknown")
        health_color = "green" if health_status == "healthy" else ("yellow" if health_status == "degraded" else "red")
        widgets.append({
            "type": "status",
            "id": "__platform_health",
            "label": "Health",
            "value": health_status.title(),
            "color": health_color,
            "platform_source": "health.status"
        })

    # Running tasks
    if exec_stats["running_count"] > 0:
        widgets.append({
            "type": "metric",
            "id": "__platform_running",
            "label": "Running",
            "value": exec_stats["running_count"],
            "color": "blue",
            "platform_source": "executions.running"
        })

    return {
        "title": "Platform Metrics",
        "description": "Automatically tracked by Trinity",
        "layout": "grid",
        "columns": 4,
        "platform_managed": True,
        "widgets": widgets
    }


def _enrich_widgets_with_history(
    config: dict,
    agent_name: str,
    hours: int = 24
) -> dict:
    """Enrich trackable widgets with historical data and trends.

    Modifies the config in-place, adding 'history' field to metric/progress/status widgets.

    Args:
        config: Dashboard configuration dict
        agent_name: Name of the agent
        hours: Time window for history

    Returns:
        The modified config dict
    """
    if not config or "sections" not in config:
        return config

    # Get all widget history at once
    all_history = db.get_all_widget_history(agent_name, hours)

    for section_idx, section in enumerate(config.get("sections", [])):
        for widget_idx, widget in enumerate(section.get("widgets", [])):
            widget_type = widget.get("type")

            # Only enrich trackable widgets
            if widget_type not in ("metric", "progress", "status"):
                continue

            # Get widget key
            widget_key = widget.get("id") or f"s{section_idx}_w{widget_idx}"

            # Get history for this widget
            history_values = all_history.get(widget_key, [])

            if len(history_values) >= 1:
                # Calculate stats
                stats = db.calculate_widget_stats(history_values)

                # Add history to widget
                widget["history"] = {
                    "values": history_values,
                    "trend": stats["trend"],
                    "trend_percent": stats["trend_percent"],
                    "min": stats["min"],
                    "max": stats["max"],
                    "avg": stats["avg"]
                }

    return config


async def get_agent_dashboard_logic(
    agent_name: str,
    current_user: User,
    include_history: bool = True,
    history_hours: int = 24,
    include_platform_metrics: bool = True
) -> dict:
    """
    Get agent dashboard configuration with optional history enrichment.

    Returns dashboard configuration from agent's dashboard.yaml file,
    enriched with historical data for sparklines and platform metrics.

    Widget types supported:
    - metric: Single numeric value with optional trend
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

    Args:
        agent_name: Name of the agent
        current_user: Authenticated user
        include_history: Whether to include historical sparkline data
        history_hours: Hours of history to include (default: 24)
        include_platform_metrics: Whether to append platform metrics section

    Returns:
    - agent_name: Name of the agent
    - has_dashboard: Whether agent has a dashboard.yaml file
    - config: Dashboard configuration (title, description, refresh, sections)
    - last_modified: File modification timestamp
    - status: Agent status (running/stopped)
    - error: Error message if parsing failed
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    await container_reload(container)

    # If agent is not running, return basic info
    if container.status != "running":
        return {
            "agent_name": agent_name,
            "has_dashboard": False,
            "config": None,
            "last_modified": None,
            "status": "stopped",
            "error": "Agent must be running to read dashboard"
        }

    # Agent is running - fetch dashboard from agent's internal API
    try:
        agent_url = f"http://agent-{agent_name}:8000/api/dashboard"
        async with agent_httpx_client(agent_name, timeout=10.0) as client:
            response = await client.get(agent_url)
            if response.status_code == 200:
                data = response.json()
                data["agent_name"] = agent_name
                data["status"] = "running"

                # If the agent returned an error (YAML parse/validation),
                # try to serve the last valid cached dashboard from DB
                if data.get("error") and not data.get("has_dashboard"):
                    return _serve_cached_or_error(
                        agent_name, data["error"], data,
                        include_history, history_hours, include_platform_metrics
                    )

                # Valid dashboard - cache to DB and process normally
                if data.get("has_dashboard") and data.get("config"):
                    db.cache_valid_dashboard(
                        agent_name,
                        data["config"],
                        data.get("last_modified")
                    )

                    # Capture snapshot if dashboard changed
                    if data.get("last_modified"):
                        last_mtime = db.get_last_captured_mtime(agent_name)
                        current_mtime = data["last_modified"]

                        if last_mtime != current_mtime:
                            db.capture_dashboard_snapshot(
                                agent_name,
                                data["config"],
                                current_mtime
                            )
                            logger.debug(f"Captured dashboard snapshot for {agent_name} (mtime: {current_mtime})")

                # Enrich with history if requested
                if include_history and data.get("has_dashboard") and data.get("config"):
                    _enrich_widgets_with_history(data["config"], agent_name, history_hours)

                # Add platform metrics section if requested
                if include_platform_metrics and data.get("has_dashboard") and data.get("config"):
                    _add_platform_metrics(data["config"], agent_name, history_hours)

                return data
            else:
                return _serve_cached_or_error(
                    agent_name, f"Failed to fetch dashboard: HTTP {response.status_code}", None,
                    include_history, history_hours, include_platform_metrics
                )
    except httpx.TimeoutException:
        return _serve_cached_or_error(
            agent_name, "Agent is starting up, please try again", None,
            include_history, history_hours, include_platform_metrics
        )
    except Exception as e:
        logger.error(f"Failed to fetch dashboard for agent {agent_name}: {e}")
        return _serve_cached_or_error(
            agent_name, f"Failed to read dashboard: {str(e)}", None,
            include_history, history_hours, include_platform_metrics
        )


def _serve_cached_or_error(
    agent_name: str,
    error_reason: str,
    fallback_data: Optional[dict],
    include_history: bool,
    history_hours: int,
    include_platform_metrics: bool
) -> dict:
    """Try to serve a DB-cached dashboard; fall back to error response."""
    cached = db.get_cached_dashboard(agent_name)
    if cached:
        stale_data = copy.deepcopy(cached)
        stale_data["agent_name"] = agent_name
        stale_data["status"] = "running"
        stale_data["stale"] = True
        stale_data["stale_reason"] = error_reason

        if include_history and stale_data.get("config"):
            _enrich_widgets_with_history(stale_data["config"], agent_name, history_hours)
        if include_platform_metrics and stale_data.get("config"):
            _add_platform_metrics(stale_data["config"], agent_name, history_hours)

        return stale_data

    # No cache available
    if fallback_data:
        return fallback_data

    return {
        "agent_name": agent_name,
        "has_dashboard": False,
        "config": None,
        "last_modified": None,
        "status": "running",
        "error": error_reason
    }


def _add_platform_metrics(config: dict, agent_name: str, hours: int) -> None:
    """Append platform metrics section to config if opted in."""
    if config.get("platform_metrics") is not False:
        platform_section = _build_platform_metrics_section(agent_name, hours)
        if platform_section["widgets"]:
            # Remove any existing platform section
            config["sections"] = [
                s for s in config.get("sections", [])
                if not s.get("platform_managed")
            ]
            config["sections"].append(platform_section)
