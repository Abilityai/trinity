"""Regression tests for #669 — `GET /api/monitoring/status` returns 500
when one or more `agent_health_checks` rows have `status = NULL`.

Root cause: the original build loop did
    AgentHealthSummary(status=check.get("status", "unknown"), ...)

`dict.get(key, default)` only returns the default when the key is *missing*.
If the key exists with a `None` value (which happens whenever a partial
health-check row is persisted), the call returns `None` and Pydantic v2
rejects the model because `AgentHealthSummary.status: str` is required and
non-optional. The whole fleet-status endpoint then 500s, even though the
sibling per-agent endpoint (`/api/monitoring/agents/{name}`) recovers by
triggering a fresh check for missing-aggregate rows.

The fix extracts a single `_build_agent_summary(name, check)` helper that
coerces `None`/missing/non-string status to `"unknown"` and tolerates a
`None` `error_message`. The endpoint also wraps the aggregation in a
defensive try/except returning an "unknown"-degraded payload (issue ask #1)
so that any future schema drift surfaces as a structured response rather
than a 500.

Issue: https://github.com/abilityai/trinity/issues/669
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

# Issue #589: src/backend/config.py raises at import unless REDIS_URL carries
# credentials. Stub it before any backend import lands.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _load_monitoring_router():
    """Load `routers.monitoring` with heavy deps stubbed.

    `routers/monitoring.py` imports `database`, `services.monitoring_service`,
    `services.agent_service`, `dependencies` — all of which transitively touch
    `/data` (SQLite path), Redis, Docker. None of that matters for the pure
    helper functions under test (`_build_agent_summary`, `_status_sort_key`).
    """
    # Stub `database` so `from database import db` returns a MagicMock.
    sys.modules.setdefault("database", types.SimpleNamespace(db=MagicMock()))

    # Stub `services` package + the two submodules monitoring.py imports.
    services_stub = types.ModuleType("services")
    services_stub.__path__ = [str(_BACKEND / "services")]
    sys.modules.setdefault("services", services_stub)
    sys.modules.setdefault(
        "services.monitoring_service",
        types.SimpleNamespace(
            perform_health_check=AsyncMock(),
            perform_fleet_health_check=AsyncMock(),
            get_monitoring_service=MagicMock(),
            start_monitoring_service=MagicMock(),
            stop_monitoring_service=MagicMock(),
            DEFAULT_CONFIG=MagicMock(),
        ),
    )
    sys.modules.setdefault(
        "services.agent_service",
        types.SimpleNamespace(get_accessible_agents=MagicMock(return_value=[])),
    )
    sys.modules.setdefault(
        "services.agent_client",
        types.SimpleNamespace(get_all_circuit_states=MagicMock(return_value={})),
    )

    # `dependencies` imports the world; stub the three names monitoring uses.
    sys.modules.setdefault(
        "dependencies",
        types.SimpleNamespace(
            get_current_user=MagicMock(),
            require_admin=MagicMock(),
            AuthorizedAgentByName=str,  # FastAPI annotation placeholder
        ),
    )

    spec = importlib.util.spec_from_file_location(
        "routers.monitoring", _BACKEND / "routers" / "monitoring.py"
    )
    routers_pkg = sys.modules.setdefault("routers", types.ModuleType("routers"))
    routers_pkg.__path__ = [str(_BACKEND / "routers")]
    mod = importlib.util.module_from_spec(spec)
    sys.modules["routers.monitoring"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fleet_helpers():
    return _load_monitoring_router()


@pytest.fixture(scope="module")
def AgentHealthSummary():
    return importlib.import_module("db_models").AgentHealthSummary


# ---------------------------------------------------------------------------
# _build_agent_summary — pure helper extracted from the build loop
# ---------------------------------------------------------------------------


class TestBuildAgentSummary:
    def test_full_check_row_passes_through(self, fleet_helpers, AgentHealthSummary):
        check = {
            "status": "healthy",
            "container_status": "running",
            "reachable": True,
            "runtime_available": True,
            "checked_at": "2026-05-06T00:00:00Z",
            "error_message": "",
        }
        s = fleet_helpers._build_agent_summary("agent-a", check)
        assert isinstance(s, AgentHealthSummary)
        assert s.name == "agent-a"
        assert s.status == "healthy"
        assert s.docker_status == "running"
        assert s.network_reachable is True
        assert s.runtime_available is True
        assert s.last_check_at == "2026-05-06T00:00:00Z"
        assert s.issues == []

    def test_status_none_coerces_to_unknown(self, fleet_helpers, AgentHealthSummary):
        """The 500 trigger from #669: row with explicit NULL status."""
        check = {
            "status": None,
            "container_status": "running",
            "reachable": True,
            "checked_at": "2026-05-06T00:00:00Z",
            "error_message": None,
        }
        s = fleet_helpers._build_agent_summary("agent-b", check)
        assert s.status == "unknown"
        assert s.issues == []  # None error_message must not crash .split

    def test_missing_status_key_coerces_to_unknown(self, fleet_helpers):
        check = {"container_status": "running"}
        s = fleet_helpers._build_agent_summary("agent-c", check)
        assert s.status == "unknown"

    def test_missing_check_returns_unknown_with_no_data_issue(self, fleet_helpers):
        """When db lookup returns no row at all (None), we still must build a
        valid AgentHealthSummary — the endpoint relied on this branch."""
        s = fleet_helpers._build_agent_summary("agent-d", None)
        assert s.status == "unknown"
        assert s.issues == ["No health check data"]

    def test_error_message_splits_on_semicolon(self, fleet_helpers):
        check = {
            "status": "degraded",
            "error_message": "context high; runtime stalled",
        }
        s = fleet_helpers._build_agent_summary("agent-e", check)
        assert s.issues == ["context high", "runtime stalled"]

    def test_non_string_status_coerces_to_unknown(self, fleet_helpers):
        """Defensive: if a row ever lands with a non-string status (e.g. int
        from a botched migration), don't 500 — degrade to unknown."""
        check = {"status": 0}
        s = fleet_helpers._build_agent_summary("agent-f", check)
        assert s.status == "unknown"


# ---------------------------------------------------------------------------
# status_order sort tolerance — sort key must not raise on coerced status.
# ---------------------------------------------------------------------------


class TestStatusOrderSort:
    def test_sort_tolerates_unknown(self, fleet_helpers):
        items = [
            fleet_helpers._build_agent_summary("a", {"status": "healthy"}),
            fleet_helpers._build_agent_summary("b", {"status": None}),
            fleet_helpers._build_agent_summary("c", {"status": "critical"}),
        ]
        items.sort(key=fleet_helpers._status_sort_key)
        assert [i.name for i in items] == ["c", "b", "a"]
