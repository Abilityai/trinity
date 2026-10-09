"""The ops cost rollup (#1028) — `GET /api/ops/costs` behind its router gate.

Split from `fleet_ops_service` so neither service sits in the critical size


class the #1028 refactor exists to empty: this is the read-side Prometheus
scrape + formatting, with no fleet mutation in reach.
"""
import logging
import os
import re
from typing import Any, Dict

import httpx
from fastapi import Request

from database import db
from utils.helpers import utc_now_iso
from models import User

logger = logging.getLogger(__name__)

OTEL_ENABLED = os.getenv("OTEL_ENABLED", "1") == "1"
OTEL_PROMETHEUS_ENDPOINT = os.getenv("OTEL_PROMETHEUS_ENDPOINT", "http://trinity-otel-collector:8889/metrics")


def parse_prometheus_metrics(text: str) -> Dict[str, Any]:
    """
    Parse Prometheus text format into structured data.

    Prometheus format:
    # HELP metric_name Description
    # TYPE metric_name counter
    metric_name{label="value"} 123.45
    """
    metrics = {
        "cost": {},        # cost by model
        "tokens": {},      # tokens by model and type
        "lines": {},       # lines added/removed
        "sessions": 0,     # session count
        "active_time": 0,  # active time in seconds
        "commits": 0,      # commit count
        "pull_requests": 0 # PR count
    }

    for line in text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        # Parse metric line: metric_name{labels} value
        # Handle metrics with and without labels
        match = re.match(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)\{([^}]*)\}\s+([0-9.eE+-]+)$', line)
        if not match:
            # Try without labels
            match = re.match(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)\s+([0-9.eE+-]+)$', line)
            if match:
                metric_name = match.group(1)
                labels = {}
                value = float(match.group(2))
            else:
                continue
        else:
            metric_name = match.group(1)
            labels_str = match.group(2)
            value = float(match.group(3))

            # Parse labels
            labels = {}
            for label_match in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"', labels_str):
                labels[label_match.group(1)] = label_match.group(2)

        # Cost metrics (trinity_claude_code_cost_usage_USD_total or trinity_cost_usage_USD_total)
        if 'cost_usage' in metric_name and 'USD' in metric_name:
            model = labels.get('model', 'unknown')
            if model not in metrics["cost"]:
                metrics["cost"][model] = 0
            metrics["cost"][model] += value

        # Token metrics (trinity_claude_code_token_usage_tokens_total or trinity_token_usage_tokens_total)
        elif 'token_usage' in metric_name and 'tokens' in metric_name:
            model = labels.get('model', 'unknown')
            token_type = labels.get('type', 'unknown')

            if model not in metrics["tokens"]:
                metrics["tokens"][model] = {}
            if token_type not in metrics["tokens"][model]:
                metrics["tokens"][model][token_type] = 0
            metrics["tokens"][model][token_type] += value

        # Lines of code
        elif 'lines_of_code' in metric_name:
            change_type = labels.get('type', labels.get('change_type', 'unknown'))
            if change_type not in metrics["lines"]:
                metrics["lines"][change_type] = 0
            metrics["lines"][change_type] += int(value)

        # Session count
        elif 'session_count' in metric_name or 'session' in metric_name.lower() and 'count' in metric_name.lower():
            metrics["sessions"] += int(value)

        # Active time
        elif 'active_time' in metric_name:
            metrics["active_time"] += value

        # Commits
        elif 'commit_count' in metric_name or 'commit' in metric_name.lower() and 'count' in metric_name.lower():
            metrics["commits"] += int(value)

        # Pull requests
        elif 'pull_request' in metric_name:
            metrics["pull_requests"] += int(value)

    return metrics


def calculate_totals(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate total values from parsed metrics."""
    total_cost = sum(metrics["cost"].values()) if metrics["cost"] else 0

    total_tokens = 0
    tokens_by_type = {}
    for model_tokens in metrics["tokens"].values():
        for token_type, count in model_tokens.items():
            total_tokens += count
            if token_type not in tokens_by_type:
                tokens_by_type[token_type] = 0
            tokens_by_type[token_type] += count

    total_lines = sum(metrics["lines"].values()) if metrics["lines"] else 0

    return {
        "total_cost": round(total_cost, 4),
        "total_tokens": int(total_tokens),
        "tokens_by_type": tokens_by_type,
        "total_lines": total_lines,
        "sessions": metrics["sessions"],
        "active_time_seconds": metrics["active_time"],
        "commits": metrics["commits"],
        "pull_requests": metrics["pull_requests"]
    }


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
        # #2726: the substitution above strips only an 8-DIGIT suffix, so a
        # point-release id keeps its `-1` and falls through to the title-case
        # fallback, rendering "Claude Fable 5 1". Every earlier catalog addition
        # degraded cleanly ("claude-opus-5" -> "Claude Opus 5"), so this is the
        # first id that needs an exact entry. `claude-fable-5` is deliberately
        # NOT mapped: its fallback is already correct, and a prefix entry for it
        # would swallow this one (startswith, first match wins).
        "claude-fable-5-1": "Claude Fable 5.1",
        # #2987: same shape one tier over — `claude-opus-5-5` keeps its trailing
        # `-5` past the 8-digit strip and would render "Claude Opus 5 5". It must
        # sit BEFORE any `claude-opus-5` prefix entry (startswith, first match
        # wins); `claude-opus-5` itself stays unmapped because its fallback is
        # already correct, and mapping it here would swallow this line.
        "claude-opus-5-5": "Claude Opus 5.5",
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
