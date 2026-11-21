"""A measured 0% uptime / 0 ms latency is reported, not "never measured" (#4685).

`GET /api/monitoring/agents/{name}` built its 24h figures with a truthiness
test, so a measured `0.0` (an agent unreachable for the whole window) came back
as `null`, which consumers render as "no reading". The handler is called
directly against a real DB (the #3950 harness).
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from db_harness import db_backend, run as _hrun  # noqa: F401

pytestmark = pytest.mark.unit


@pytest.fixture
def db(db_backend):  # noqa: F811
    from database import db as database

    return database


def _agent(monkeypatch, db, name):
    mon = importlib.import_module("routers.monitoring")
    view = importlib.import_module("services.circuit_breaker_view")
    monkeypatch.setattr(mon, "db", db)
    monkeypatch.setattr(
        mon,
        "get_monitoring_service",
        lambda: SimpleNamespace(
            is_running=True, config=SimpleNamespace(docker_check_interval=30)
        ),
    )
    monkeypatch.setattr(mon, "_declared_metrics_block", lambda n: None)
    monkeypatch.setattr(view, "build_circuit_breaker_block", lambda n: None)
    return asyncio.run(mon.get_agent_health(name))


def test_zero_uptime_and_zero_latency_are_reported_as_zero(db, monkeypatch):
    db.create_health_check("z-down", "aggregate", "unhealthy", error_message="unreachable")
    db.create_health_check("z-down", "network", "unhealthy", network_metrics={"reachable": True, "latency_ms": 0.0})

    body = _agent(monkeypatch, db, "z-down")

    assert body.uptime_percent_24h == 0.0
    assert body.uptime_percent_24h is not None
    assert body.avg_latency_24h_ms == 0.0
    assert body.avg_latency_24h_ms is not None


def test_no_checks_in_the_window_still_reads_none(db, monkeypatch):
    # A latest aggregate row outside the 24h window: the handler is reached
    # (a row exists) but the windowed figures have nothing to average.
    db.create_health_check("z-old", "aggregate", "healthy")
    _hrun(
        "UPDATE agent_health_checks SET checked_at = :t WHERE agent_name = :n",
        t=(datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        n="z-old",
    )

    body = _agent(monkeypatch, db, "z-old")

    assert body.uptime_percent_24h is None
    assert body.avg_latency_24h_ms is None
