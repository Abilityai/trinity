"""
Observability routes for the Trinity backend.

Provides endpoints for retrieving OpenTelemetry metrics from the OTEL Collector.
Metrics are exposed in structured JSON format for dashboard consumption.
"""
import os
import re
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Depends, HTTPException
import httpx

from models import User
from dependencies import get_current_user

router = APIRouter(prefix="/api/observability", tags=["observability"])

# Configuration from environment (enabled by default)
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


@router.get("/metrics")
async def get_observability_metrics(
    current_user: User = Depends(get_current_user)
):
    """
    Get OpenTelemetry metrics from the OTEL Collector.

    Returns structured metrics data including:
    - Cost breakdown by model
    - Token usage by model and type
    - Productivity metrics (lines, sessions, commits, PRs)

    Returns {enabled: false} when OTel is not configured.
    """
    if not OTEL_ENABLED:
        return {
            "enabled": False,
            "message": "OpenTelemetry is not enabled. Set OTEL_ENABLED=1 to enable."
        }

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
                    "error": f"Collector returned status {response.status_code}",
                    "metrics": None,
                    "totals": None
                }

            # Parse Prometheus format
            metrics = parse_prometheus_metrics(response.text)
            totals = calculate_totals(metrics)

            return {
                "enabled": True,
                "available": True,
                "metrics": {
                    "cost_by_model": metrics["cost"],
                    "tokens_by_model": metrics["tokens"],
                    "lines_of_code": metrics["lines"],
                    "sessions": metrics["sessions"],
                    "active_time_seconds": metrics["active_time"],
                    "commits": metrics["commits"],
                    "pull_requests": metrics["pull_requests"]
                },
                "totals": totals
            }

    except httpx.ConnectError:
        return {
            "enabled": True,
            "available": False,
            "error": "Cannot connect to OTEL Collector. Is it running?",
            "metrics": None,
            "totals": None
        }
    except httpx.TimeoutException:
        return {
            "enabled": True,
            "available": False,
            "error": "OTEL Collector request timed out",
            "metrics": None,
            "totals": None
        }
    except Exception as e:
        return {
            "enabled": True,
            "available": False,
            "error": f"Failed to fetch metrics: {str(e)}",
            "metrics": None,
            "totals": None
        }


@router.get("/status")
async def get_observability_status(
    current_user: User = Depends(get_current_user)
):
    """
    Get the status of the OpenTelemetry integration.

    Quick check without full metrics parsing.
    """
    if not OTEL_ENABLED:
        return {
            "enabled": False,
            "collector_configured": False,
            "collector_reachable": False
        }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                OTEL_PROMETHEUS_ENDPOINT,
                timeout=2.0
            )

            return {
                "enabled": True,
                "collector_configured": True,
                "collector_reachable": response.status_code == 200,
                "endpoint": OTEL_PROMETHEUS_ENDPOINT
            }
    except Exception:
        return {
            "enabled": True,
            "collector_configured": True,
            "collector_reachable": False,
            "endpoint": OTEL_PROMETHEUS_ENDPOINT
        }
