"""#3434 — routes nothing calls stay removed.

Nine routes had no caller in any client this repo ships (frontend, MCP server,
scheduler, agent server, CLI, scripts, compose healthchecks):

* five secret-gated internal routes (`X-Internal-Secret`, router-level
  `verify_internal_secret`) — the scheduler stopped calling the activity pair
  long ago and reads sync health from the DB directly; the share route was
  superseded by `POST /api/agents/{name}/shared-files`;
* two per-agent activity reads in `routers/agents.py`, duplicated by the live
  `GET /api/activities/timeline`;
* `routers/observability.py`, whose only caller was an orphan frontend store,
  duplicated by the live admin `GET /api/ops/costs`.

Both halves are checked: the AST census (a handler re-added under the same
name) and the LIVE route table of the real app (a re-add under any name, on
the same method + path). The two OTel parse helpers that lived in the deleted
router are pinned at their new home, `services/ops_costs_service.py`, where
`GET /api/ops/costs` calls them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _route_census as rc  # noqa: E402

pytestmark = pytest.mark.unit

REMOVED = {
    "routers/internal.py::internal_health": "GET /api/internal/health",
    "routers/internal.py::track_activity": "POST /api/internal/activities/track",
    "routers/internal.py::complete_activity": "POST /api/internal/activities/{activity_id}/complete",
    "routers/internal.py::internal_agent_sync_health": "GET /api/internal/agents/{agent_name}/sync-health-status",
    "routers/internal.py::agent_files_share": "POST /api/internal/agent-files/share",
    "routers/agents.py::get_agent_activities": "GET /api/agents/{agent_name}/activities",
    "routers/agents.py::get_activity_timeline": "GET /api/agents/activities/timeline",
    "routers/observability.py::get_observability_metrics": "GET /api/observability/metrics",
    "routers/observability.py::get_observability_status": "GET /api/observability/status",
}


def test_removed_handlers_absent_from_ast_census():
    present = sorted(k for k in REMOVED if k in rc.walk().routes)
    assert present == [], f"removed routes are back in the source: {present}"


def test_removed_paths_absent_from_live_route_table():
    live = {p for paths in rc.runtime_paths(rc.runtime_routes()).values() for p in paths}
    present = sorted(p for p in REMOVED.values() if p in live)
    assert present == [], f"removed routes are registered by the real app: {present}"


def test_observability_router_module_is_gone():
    assert not (rc.BACKEND / "routers" / "observability.py").exists()


SAMPLE = "\n".join([
    "# HELP trinity_claude_code_cost_usage_USD_total Cost",
    "# TYPE trinity_claude_code_cost_usage_USD_total counter",
    'trinity_claude_code_cost_usage_USD_total{model="claude-opus-5"} 1.5',
    'trinity_claude_code_cost_usage_USD_total{model="claude-sonnet-5"} 0.25',
    'trinity_claude_code_token_usage_tokens_total{model="claude-opus-5",type="input"} 100',
    'trinity_claude_code_token_usage_tokens_total{model="claude-opus-5",type="output"} 40',
    'trinity_claude_code_token_usage_tokens_total{model="claude-sonnet-5",type="input"} 10',
    'trinity_claude_code_lines_of_code_count_total{type="added"} 12',
    'trinity_claude_code_lines_of_code_count_total{type="removed"} 3',
    "trinity_claude_code_session_count_total 4",
    "trinity_claude_code_active_time_seconds_total 90.5",
    "trinity_claude_code_commit_count_total 2",
    "trinity_claude_code_pull_request_count_total 1",
    "garbage line that is not a metric",
    "",
])


def test_parse_helpers_live_in_ops_costs_service():
    from services.ops_costs_service import calculate_totals, parse_prometheus_metrics

    metrics = parse_prometheus_metrics(SAMPLE)
    assert metrics["cost"] == {"claude-opus-5": 1.5, "claude-sonnet-5": 0.25}
    assert metrics["tokens"]["claude-opus-5"] == {"input": 100, "output": 40}
    assert metrics["lines"] == {"added": 12, "removed": 3}
    assert metrics["sessions"] == 4
    assert metrics["active_time"] == 90.5
    assert metrics["commits"] == 2
    assert metrics["pull_requests"] == 1

    totals = calculate_totals(metrics)
    assert totals["total_cost"] == 1.75
    assert totals["total_tokens"] == 150
    assert totals["tokens_by_type"] == {"input": 110, "output": 40}
    assert totals["total_lines"] == 15


def test_parse_helpers_empty_input():
    from services.ops_costs_service import calculate_totals, parse_prometheus_metrics

    totals = calculate_totals(parse_prometheus_metrics(""))
    assert totals["total_cost"] == 0
    assert totals["total_tokens"] == 0
    assert totals["tokens_by_type"] == {}
