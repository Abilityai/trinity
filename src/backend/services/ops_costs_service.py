"""The ops cost rollup (#1028) — `GET /api/ops/costs` behind its router gate.

Split from `fleet_ops_service` so neither service sits in the critical size
class the #1028 refactor exists to empty: this is the read-side Prometheus
scrape + formatting, with no fleet mutation in reach.
"""
import logging
import os

import httpx
from fastapi import Request

from database import db
from utils.helpers import utc_now_iso
from models import User

logger = logging.getLogger(__name__)

OTEL_ENABLED = os.getenv("OTEL_ENABLED", "1") == "1"
OTEL_PROMETHEUS_ENDPOINT = os.getenv("OTEL_PROMETHEUS_ENDPOINT", "http://trinity-otel-collector:8889/metrics")

def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes}m"
    else:
        hours = int(seconds / 3600)
        minutes = int((seconds % 3600) / 60)
        if minutes > 0:
            return f"{hours}h {minutes}m"
        return f"{hours}h"


def _format_model_name(model_id: str) -> str:
    """Format a model ID into a human-readable name."""
    if not model_id:
        return "Unknown"

    # Remove date suffixes like -20250514
    import re
    clean = re.sub(r'-\d{8}$', '', model_id)

    # Map common model IDs
    mappings = {
        "claude-sonnet-4": "Claude Sonnet 4",
        "claude-opus-4": "Claude Opus 4",
        "claude-haiku-4": "Claude Haiku 4",
        "claude-3-5-sonnet": "Claude 3.5 Sonnet",
        "claude-3-sonnet": "Claude 3 Sonnet",
        "claude-3-haiku": "Claude 3 Haiku",
        "claude-3-opus": "Claude 3 Opus",
    }

    for prefix, name in mappings.items():
        if clean.startswith(prefix):
            return name

    # Fallback: Title case with hyphens as spaces
    return clean.replace("-", " ").title()


async def get_ops_costs_impl(
    request: Request,
    current_user: User
):
    """
    Get cost and usage metrics for platform operations.

    Admin-only. Returns OTel metrics including cost breakdown,
    token usage, and productivity metrics.
    """
    # #2323 — per-route opt-in for the bounded read-only `ops` key.
    # `ADMIN_GATE_SCOPES` keeps ops keys out of admin gates by default, so a
    # NEW ops route is inaccessible until someone adds this — the failure
    # direction we want. Only GET routes carry it; every write below stays bare.
    # auth: the route gate ran assert_admin before delegating (#1028)

    if not OTEL_ENABLED:
        return {
            "enabled": False,
            "message": "OpenTelemetry is not enabled. Set OTEL_ENABLED=1 in your environment to enable cost tracking.",
            "setup_instructions": [
                "1. Set OTEL_ENABLED=1 in .env file",
                "2. Deploy the OTel collector (docker-compose up otel-collector)",
                "3. Restart agents to begin collecting metrics",
                "4. Wait 60 seconds for initial metrics to appear"
            ]
        }

    # Get ops settings for thresholds
    daily_cost_limit = float(db.get_setting("ops_cost_limit_daily_usd") or 50.0)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                OTEL_PROMETHEUS_ENDPOINT,
                timeout=5.0
            )

            if response.status_code != 200:
                return {
                    "enabled": True,
                    "available": False,
                    "error": f"OTel Collector returned status {response.status_code}",
                    "timestamp": utc_now_iso()
                }

            # Parse Prometheus metrics
            from routers.observability import parse_prometheus_metrics, calculate_totals
            metrics = parse_prometheus_metrics(response.text)
            totals = calculate_totals(metrics)

            # Calculate alerts based on thresholds
            alerts = []
            total_cost = totals.get("total_cost", 0)

            if daily_cost_limit > 0 and total_cost >= daily_cost_limit:
                alerts.append({
                    "severity": "critical",
                    "type": "cost_limit_exceeded",
                    "message": f"Daily cost limit exceeded: ${total_cost:.4f} >= ${daily_cost_limit:.2f}",
                    "recommendation": "Consider pausing schedules or stopping non-essential agents"
                })
            elif daily_cost_limit > 0 and total_cost >= daily_cost_limit * 0.8:
                alerts.append({
                    "severity": "warning",
                    "type": "cost_limit_approaching",
                    "message": f"Approaching daily cost limit: ${total_cost:.4f} (limit: ${daily_cost_limit:.2f})",
                    "recommendation": "Monitor closely and prepare to reduce activity if needed"
                })

            # Format cost breakdown by model
            cost_by_model = []
            for model, cost in sorted(metrics.get("cost", {}).items(), key=lambda x: x[1], reverse=True):
                # Get token counts for this model
                model_tokens = metrics.get("tokens", {}).get(model, {})
                cost_by_model.append({
                    "model": _format_model_name(model),
                    "model_id": model,
                    "cost": round(cost, 4),
                    "input_tokens": int(model_tokens.get("input", 0)),
                    "output_tokens": int(model_tokens.get("output", 0)),
                    "cache_read_tokens": int(model_tokens.get("cacheRead", 0)),
                    "cache_creation_tokens": int(model_tokens.get("cacheCreation", 0))
                })

            # Build response
            result = {
                "enabled": True,
                "available": True,
                "timestamp": utc_now_iso(),

                # Summary
                "summary": {
                    "total_cost": round(total_cost, 4),
                    "total_tokens": totals.get("total_tokens", 0),
                    "daily_limit": daily_cost_limit if daily_cost_limit > 0 else None,
                    "cost_percent_of_limit": round(total_cost / daily_cost_limit * 100, 1) if daily_cost_limit > 0 else None
                },

                # Alerts
                "alerts": alerts,

                # Detailed breakdown
                "cost_by_model": cost_by_model,

                # Token breakdown by type
                "tokens_by_type": totals.get("tokens_by_type", {}),

                # Productivity metrics
                "productivity": {
                    "sessions": totals.get("sessions", 0),
                    "active_time_seconds": totals.get("active_time_seconds", 0),
                    "active_time_formatted": _format_duration(totals.get("active_time_seconds", 0)),
                    "commits": totals.get("commits", 0),
                    "pull_requests": totals.get("pull_requests", 0),
                    "lines_added": metrics.get("lines", {}).get("added", 0),
                    "lines_removed": metrics.get("lines", {}).get("removed", 0)
                }
            }

            return result

    except httpx.ConnectError:
        return {
            "enabled": True,
            "available": False,
            "error": "Cannot connect to OTel Collector. Is it running?",
            "timestamp": utc_now_iso()
        }
    except httpx.TimeoutException:
        return {
            "enabled": True,
            "available": False,
            "error": "OTel Collector request timed out",
            "timestamp": utc_now_iso()
        }
    except Exception as e:
        logger.error(f"Failed to fetch cost metrics: {e}", exc_info=True)
        return {
            "enabled": True,
            "available": False,
            # py/stack-trace-exposure (#1917, the PR #1912 pattern). The
            # collector URL and its internal host live in this message.
            "error": (
                f"Failed to fetch metrics ({e.__class__.__name__} — "
                f"details in backend logs)"
            ),
            "timestamp": utc_now_iso()
        }
