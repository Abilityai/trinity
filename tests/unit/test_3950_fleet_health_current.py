"""Fleet health counts every agent and never serves an old verdict as current (#3950).

`GET /api/monitoring/status` (and MCP `get_fleet_health` over it) answered a
45-agent fleet with `healthy: 12, unknown: 0` while 33 agents had no health row:
the buckets were aggregated from the rows that exist, the list from the agents
that exist. And every row it did have read `healthy` 1-6 days after it was
taken, with the monitoring loop off.

Fixed shape, pinned here against a real DB:
  * the buckets are counted from the per-agent list, so they total
    `total_agents`, and a missing row is `unknown` in both halves;
  * a reading older than `stale_after_seconds` (3x the check interval, the
    monitoring leader-lease rule) reads `unknown`, says how old it is and what
    it last said, and names a stopped monitor; the per-agent route agrees;
  * a fresh reading — including a fresh fault — is untouched.

The handlers are called directly; Docker, Redis and accessibility lookups are
stubbed on the module the handler resolves them from (the ent#707 harness).
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from db_harness import db_backend, run as _hrun  # noqa: F401

pytestmark = pytest.mark.unit

def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def db(db_backend):  # noqa: F811
    from database import db as database

    return database


def _reading(db, name, status, *, age=timedelta(0), error=None):
    db.create_health_check(name, "aggregate", status, error_message=error)
    _hrun(
        "UPDATE agent_health_checks SET checked_at = :t WHERE agent_name = :n",
        # Taken now, not at import: a slow suite would otherwise age a "fresh"
        # reading past the 90 s bound before the test runs.
        t=_iso(datetime.now(timezone.utc) - age),
        n=name,
    )


def _service(*, running=True, interval=30):
    return SimpleNamespace(
        is_running=running, config=SimpleNamespace(docker_check_interval=interval)
    )


def _fleet(monkeypatch, db, names, **service):
    mon = importlib.import_module("routers.monitoring")
    docker_service = importlib.import_module("services.docker_service")
    heartbeat_service = importlib.import_module("services.heartbeat_service")
    monkeypatch.setattr(mon, "db", db)
    monkeypatch.setattr(mon, "get_monitoring_service", lambda: _service(**service))
    monkeypatch.setattr(
        mon, "get_accessible_agents", lambda user: [{"name": n} for n in names]
    )
    monkeypatch.setattr(
        docker_service,
        "list_all_agents_fast",
        lambda: [SimpleNamespace(name=n) for n in names],
    )
    monkeypatch.setattr(heartbeat_service, "heartbeat_status_bulk", lambda n: {})
    return asyncio.run(mon.get_fleet_status(current_user=SimpleNamespace(role="user")))


def _agent(monkeypatch, db, name, **service):
    mon = importlib.import_module("routers.monitoring")
    view = importlib.import_module("services.circuit_breaker_view")
    monkeypatch.setattr(mon, "db", db)
    monkeypatch.setattr(mon, "get_monitoring_service", lambda: _service(**service))
    monkeypatch.setattr(mon, "_declared_metrics_block", lambda n: None)
    monkeypatch.setattr(view, "build_circuit_breaker_block", lambda n: None)
    return asyncio.run(mon.get_agent_health(name))


def _by_name(body):
    return {a.name: a for a in body.agents}


def _bucket_total(summary):
    return (summary.healthy + summary.degraded + summary.unhealthy
            + summary.critical + summary.unknown)


# ── Defect 1: the buckets total the fleet ──────────────────────────────────


def test_an_agent_with_no_row_is_counted_unknown(db, monkeypatch):
    _reading(db, "a-measured", "healthy")
    body = _fleet(monkeypatch, db, ["a-measured", "a-never-1", "a-never-2"])

    assert body.summary.total_agents == 3
    assert body.summary.healthy == 1
    assert body.summary.unknown == 2
    assert _by_name(body)["a-never-1"].status == "unknown"


def test_the_buckets_always_total_the_fleet(db, monkeypatch):
    _reading(db, "m-ok", "healthy")
    _reading(db, "m-slow", "degraded", error="latency high")
    _reading(db, "m-down", "unhealthy", error="unreachable")
    _reading(db, "m-dead", "critical", error="oom")
    _reading(db, "m-old", "healthy", age=timedelta(days=2))
    _reading(db, "m-odd", "exotic-status")
    names = ["m-ok", "m-slow", "m-down", "m-dead", "m-old", "m-odd", "m-none"]

    body = _fleet(monkeypatch, db, names)

    assert _bucket_total(body.summary) == body.summary.total_agents == len(names)
    listed = {}
    for a in body.agents:
        bucket = a.status if a.status in ("healthy", "degraded", "unhealthy", "critical") else "unknown"
        listed[bucket] = listed.get(bucket, 0) + 1
    for bucket in ("healthy", "degraded", "unhealthy", "critical", "unknown"):
        assert getattr(body.summary, bucket) == listed.get(bucket, 0), bucket


# ── Defect 2: a verdict expires ────────────────────────────────────────────


def test_a_days_old_healthy_reading_is_not_served_as_current(db, monkeypatch):
    _reading(db, "s-old", "healthy", age=timedelta(days=6, minutes=5))
    body = _fleet(monkeypatch, db, ["s-old"])

    agent = _by_name(body)["s-old"]
    assert agent.status == "unknown"
    assert agent.issues[0] == (
        "Last health check 6d ago (reported healthy) — not a current reading"
    )
    assert agent.last_check_at is not None  # the timestamp is still exposed
    assert body.summary.healthy == 0 and body.summary.unknown == 1
    assert body.stale_after_seconds == 90


def test_a_fresh_reading_keeps_its_verdict_and_issues(db, monkeypatch):
    _reading(db, "f-ok", "healthy", age=timedelta(seconds=20))
    _reading(db, "f-down", "unhealthy", age=timedelta(seconds=20), error="unreachable")
    body = _fleet(monkeypatch, db, ["f-ok", "f-down"])

    agents = _by_name(body)
    assert agents["f-ok"].status == "healthy" and agents["f-ok"].issues == []
    assert agents["f-down"].status == "unhealthy"
    assert agents["f-down"].issues == ["unreachable"]
    assert body.summary.unhealthy == 1


def test_an_expired_fault_keeps_its_old_issues_behind_the_age(db, monkeypatch):
    _reading(db, "e-down", "unhealthy", age=timedelta(hours=3), error="unreachable")
    agent = _by_name(_fleet(monkeypatch, db, ["e-down"]))["e-down"]

    assert agent.status == "unknown"
    assert agent.issues == [
        "Last health check 3h ago (reported unhealthy) — not a current reading",
        "unreachable",
    ]


def test_the_bound_follows_the_configured_check_interval(db, monkeypatch):
    _reading(db, "i-ten-min", "healthy", age=timedelta(minutes=10))
    body = _fleet(monkeypatch, db, ["i-ten-min"], interval=600)

    assert body.stale_after_seconds == 1800
    assert _by_name(body)["i-ten-min"].status == "healthy"


# ── Defect 3: say when the instrument is off ───────────────────────────────


def test_a_stopped_monitor_is_named_on_each_expired_reading(db, monkeypatch):
    _reading(db, "o-old", "healthy", age=timedelta(days=1, hours=2))
    body = _fleet(monkeypatch, db, ["o-old"], running=False)

    assert body.enabled is False
    assert _by_name(body)["o-old"].issues[0] == (
        "Last health check 26h ago (reported healthy) — not a current reading;"
        " monitoring is off"
    )


def test_an_unreadable_timestamp_fails_closed():
    mon = importlib.import_module("routers.monitoring")
    issue = mon._expire_reading("healthy", "not-a-time", stale_after=90,
                                monitoring_running=True, now=datetime.now(timezone.utc))
    assert issue == "Last health check time unreadable — not a current reading"


# ── The per-agent route agrees ─────────────────────────────────────────────


def test_the_agent_route_expires_an_old_verdict(db, monkeypatch):
    _reading(db, "d-old", "healthy", age=timedelta(days=5))
    detail = _agent(monkeypatch, db, "d-old")

    assert detail.aggregate_status == "unknown"
    assert detail.issues[0] == (
        "Last health check 5d ago (reported healthy) — not a current reading"
    )
    assert detail.stale_after_seconds == 90


def test_the_agent_route_keeps_a_fresh_verdict(db, monkeypatch):
    _reading(db, "d-new", "healthy", age=timedelta(seconds=10))
    detail = _agent(monkeypatch, db, "d-new")

    assert detail.aggregate_status == "healthy"
    assert detail.issues == []
