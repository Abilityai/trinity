"""ent#326 — `GET /api/executions/timeline`, bucketed fleet execution rollups.

The backend half of ent#94's grid-widget foundation. Three tile sub-issues
(#96 executions-by-trigger, #98 fleet cost, #101 fleet context) read one
endpoint rather than each growing its own query.

It is the time-series sibling of `/api/executions/stats`: same table, same
access model, buckets instead of scalars. What differs from its sibling — and
what most of this file pins — is that an ANALYTICS AXIS cannot degrade the way a
FILTER can. `/stats` and the list route coerce an unknown `hours` to a default,
which is right for a filter (worst case: more rows than you asked for) and wrong
for a chart, where it silently redraws the window the caller did not request.

Also pinned: the "tokens" question ent#326 requires settling. `schedule_executions`
has NO token columns — only `cost`, `context_used`, `context_max`. So the endpoint
reports **context-window occupancy** under a field named `context_used`, and #101's
tile must be labelled to match. Anything else is the liveness-vs-quality mislabel
the issue names.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


# Column/field names that would mean "token usage was measured". Deliberately
# an explicit set: `claim_token` contains the substring "token" and is a lease
# CAS value (#1081), so a substring test answers the wrong question.
_USAGE_TOKEN_NAMES = {
    "tokens", "total_tokens", "input_tokens", "output_tokens",
    "prompt_tokens", "completion_tokens", "token_count", "tokens_used",
}


def _utc_iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture
def router_mod():
    try:
        from routers import executions as mod
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return mod


# ---------------------------------------------------------------------------
# Validation — a named 422, never a 500 or a silently-different window.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["week", "HOUR", "", "day; DROP TABLE", "minute", "status"]
)
def test_unknown_group_by_is_a_named_422(router_mod, bad):
    import asyncio
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(router_mod.get_fleet_execution_timeline(
            group_by=bad, hours=168, agent=None, current_user=_Admin(),
        ))
    assert exc.value.status_code == 422
    assert bad in str(exc.value.detail) or not bad
    assert "group_by" in str(exc.value.detail)


@pytest.mark.parametrize("bad", [5, 100, -1, 99999])
def test_unknown_hours_is_a_named_422_not_a_silent_default(router_mod, bad):
    """The sibling endpoints coerce a bad `hours` to 24. Correct for a filter,
    wrong for an axis: the caller would get a chart of a window they did not
    ask for and no way to tell."""
    import asyncio
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(router_mod.get_fleet_execution_timeline(
            group_by="day", hours=bad, agent=None, current_user=_Admin(),
        ))
    assert exc.value.status_code == 422
    assert "hours" in str(exc.value.detail)


def test_all_time_is_refused_for_gap_filled_groupings(router_mod):
    """`hours=0` is legal on `/stats` (a scalar has no axis). Here it would emit
    one bucket per interval since the fleet's first execution — an unbounded
    response nobody asked for."""
    import asyncio
    from fastapi import HTTPException

    for gb in ("hour", "day"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(router_mod.get_fleet_execution_timeline(
                group_by=gb, hours=0, agent=None, current_user=_Admin(),
            ))
        assert exc.value.status_code == 422
        assert "bounded window" in str(exc.value.detail)


def test_all_time_is_allowed_for_categorical_groupings(router_mod, monkeypatch):
    """`trigger`/`agent` have no continuum, so all-time is bounded by the number
    of distinct triggers/agents — refusing it would be arbitrary."""
    import asyncio

    monkeypatch.setattr(router_mod.db, "get_fleet_execution_timeline",
                        lambda *a, **k: [], raising=False)
    monkeypatch.setattr(router_mod, "accessible_agent_names", lambda u: None)
    monkeypatch.setattr(router_mod, "narrow_to_agent", lambda names, a: names)

    for gb in ("trigger", "agent"):
        out = asyncio.run(router_mod.get_fleet_execution_timeline(
            group_by=gb, hours=0, agent=None, current_user=_Admin(),
        ))
        assert out.hours == 0
        assert out.gap_filled is False


# ---------------------------------------------------------------------------
# Gap-fill — a zero bucket and a missing bucket mean different things.
# ---------------------------------------------------------------------------


def test_day_series_is_continuous_even_with_no_data(router_mod, monkeypatch):
    import asyncio

    monkeypatch.setattr(router_mod.db, "get_fleet_execution_timeline",
                        lambda *a, **k: [], raising=False)
    monkeypatch.setattr(router_mod, "accessible_agent_names", lambda u: None)
    monkeypatch.setattr(router_mod, "narrow_to_agent", lambda names, a: names)

    out = asyncio.run(router_mod.get_fleet_execution_timeline(
        group_by="day", hours=168, agent=None, current_user=_Admin(),
    ))
    assert out.gap_filled is True
    # 7 days back through today, inclusive.
    assert len(out.buckets) >= 8
    assert all(b.total == 0 for b in out.buckets)
    keys = [b.bucket for b in out.buckets]
    assert keys == sorted(keys), "the axis must be chronologically ordered"
    assert len(set(keys)) == len(keys), "no duplicate buckets"


def test_a_populated_day_keeps_its_values_and_neighbours_are_zero(router_mod, monkeypatch):
    import asyncio

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    monkeypatch.setattr(
        router_mod.db, "get_fleet_execution_timeline",
        lambda *a, **k: [{"bucket": today, "total": 3, "success": 2, "failed": 1,
                          "cost": 0.5, "context_used": 900}],
        raising=False,
    )
    monkeypatch.setattr(router_mod, "accessible_agent_names", lambda u: None)
    monkeypatch.setattr(router_mod, "narrow_to_agent", lambda names, a: names)

    out = asyncio.run(router_mod.get_fleet_execution_timeline(
        group_by="day", hours=168, agent=None, current_user=_Admin(),
    ))
    hit = [b for b in out.buckets if b.bucket == today]
    assert len(hit) == 1
    assert (hit[0].total, hit[0].success, hit[0].failed) == (3, 2, 1)
    assert hit[0].cost == 0.5 and hit[0].context_used == 900
    assert all(b.total == 0 for b in out.buckets if b.bucket != today)


def test_hour_series_uses_hour_granularity(router_mod, monkeypatch):
    import asyncio

    monkeypatch.setattr(router_mod.db, "get_fleet_execution_timeline",
                        lambda *a, **k: [], raising=False)
    monkeypatch.setattr(router_mod, "accessible_agent_names", lambda u: None)
    monkeypatch.setattr(router_mod, "narrow_to_agent", lambda names, a: names)

    out = asyncio.run(router_mod.get_fleet_execution_timeline(
        group_by="hour", hours=24, agent=None, current_user=_Admin(),
    ))
    assert 24 <= len(out.buckets) <= 26
    assert all(len(b.bucket) == 13 and b.bucket[10] == "T" for b in out.buckets)


def test_categorical_groupings_are_not_gap_filled(router_mod, monkeypatch):
    """There is no continuum of agents to fill — inventing empty buckets would
    invent agents."""
    import asyncio

    monkeypatch.setattr(
        router_mod.db, "get_fleet_execution_timeline",
        lambda *a, **k: [{"bucket": "a1", "total": 1, "success": 1, "failed": 0,
                          "cost": 0.0, "context_used": 0}],
        raising=False,
    )
    monkeypatch.setattr(router_mod, "accessible_agent_names", lambda u: None)
    monkeypatch.setattr(router_mod, "narrow_to_agent", lambda names, a: names)

    out = asyncio.run(router_mod.get_fleet_execution_timeline(
        group_by="agent", hours=168, agent=None, current_user=_Admin(),
    ))
    assert out.gap_filled is False
    assert [b.bucket for b in out.buckets] == ["a1"]


# ---------------------------------------------------------------------------
# Trigger folding — the "Other" catch-all is the point.
# ---------------------------------------------------------------------------


def test_unknown_trigger_folds_to_other_instead_of_vanishing(router_mod, monkeypatch):
    """The whole reason folding happens in Python: `_TRIGGER_BUCKETS` has an
    explicit `Other`. A SQL CASE would drop a newly-added trigger silently the
    first time someone forgets to update it."""
    import asyncio

    monkeypatch.setattr(
        router_mod.db, "get_fleet_execution_timeline",
        lambda *a, **k: [
            {"bucket": "schedule", "total": 2, "success": 2, "failed": 0,
             "cost": 0.1, "context_used": 10},
            {"bucket": "brand_new_trigger_type", "total": 5, "success": 5,
             "failed": 0, "cost": 0.2, "context_used": 20},
        ],
        raising=False,
    )
    monkeypatch.setattr(router_mod, "accessible_agent_names", lambda u: None)
    monkeypatch.setattr(router_mod, "narrow_to_agent", lambda names, a: names)

    out = asyncio.run(router_mod.get_fleet_execution_timeline(
        group_by="trigger", hours=168, agent=None, current_user=_Admin(),
    ))
    labels = {b.bucket: b for b in out.buckets}
    assert "Other" in labels, "an unmapped trigger vanished from the chart"
    assert labels["Other"].total == 5
    assert sum(b.total for b in out.buckets) == 7, "no execution may be lost"


def test_triggers_sharing_a_bucket_are_summed(router_mod, monkeypatch):
    import asyncio

    from db.schedules import _TRIGGER_BUCKETS

    same = [t for t, b in _TRIGGER_BUCKETS.items()
            if b == _TRIGGER_BUCKETS.get("manual")][:2]
    if len(same) < 2:
        pytest.skip("no two triggers share a bucket in this mapping")

    monkeypatch.setattr(
        router_mod.db, "get_fleet_execution_timeline",
        lambda *a, **k: [
            {"bucket": same[0], "total": 2, "success": 1, "failed": 1,
             "cost": 0.25, "context_used": 5},
            {"bucket": same[1], "total": 3, "success": 3, "failed": 0,
             "cost": 0.25, "context_used": 7},
        ],
        raising=False,
    )
    monkeypatch.setattr(router_mod, "accessible_agent_names", lambda u: None)
    monkeypatch.setattr(router_mod, "narrow_to_agent", lambda names, a: names)

    out = asyncio.run(router_mod.get_fleet_execution_timeline(
        group_by="trigger", hours=168, agent=None, current_user=_Admin(),
    ))
    merged = [b for b in out.buckets if b.bucket == _TRIGGER_BUCKETS[same[0]]]
    assert len(merged) == 1, "same-bucket triggers must merge, not duplicate"
    assert merged[0].total == 5 and merged[0].cost == 0.5
    assert merged[0].failed == 1 and merged[0].context_used == 12


# ---------------------------------------------------------------------------
# Access scoping — identical to /api/executions.
# ---------------------------------------------------------------------------


def test_non_admin_scope_is_passed_to_the_db(router_mod, monkeypatch):
    import asyncio

    seen = {}

    def _capture(agent_names, group_by, hours):
        seen["names"] = agent_names
        return []

    monkeypatch.setattr(router_mod.db, "get_fleet_execution_timeline", _capture,
                        raising=False)
    monkeypatch.setattr(router_mod, "accessible_agent_names", lambda u: ["mine"])
    monkeypatch.setattr(router_mod, "narrow_to_agent", lambda names, a: names)

    asyncio.run(router_mod.get_fleet_execution_timeline(
        group_by="day", hours=24, agent=None, current_user=_User(),
    ))
    assert seen["names"] == ["mine"], "the caller's scope must reach the query"


def test_admin_scope_is_none_meaning_no_filter(router_mod, monkeypatch):
    import asyncio

    seen = {}
    monkeypatch.setattr(
        router_mod.db, "get_fleet_execution_timeline",
        lambda agent_names, group_by, hours: seen.setdefault("names", agent_names) or [],
        raising=False,
    )
    monkeypatch.setattr(router_mod, "accessible_agent_names", lambda u: None)
    monkeypatch.setattr(router_mod, "narrow_to_agent", lambda names, a: names)

    asyncio.run(router_mod.get_fleet_execution_timeline(
        group_by="day", hours=24, agent=None, current_user=_Admin(),
    ))
    assert seen["names"] is None


def test_a_user_with_no_accessible_agents_gets_an_empty_series_not_everything(router_mod):
    """The dangerous direction. An empty allow-list must mean "nothing", never
    "no filter" — the db layer short-circuits before building any SQL."""
    from db.schedules.stats import ScheduleStatsMixin

    rows = ScheduleStatsMixin.get_fleet_execution_timeline(
        object(), [], group_by="day", hours=24,
    )
    assert rows == []


# ---------------------------------------------------------------------------
# Route ordering (Invariant #4).
# ---------------------------------------------------------------------------


def test_timeline_is_registered_before_the_catch_all(router_mod):
    """`/stats` already sits under this rule; without it `timeline` is readable
    as an execution id."""
    paths = [r.path for r in router_mod.router.routes]
    assert "/api/executions/timeline" in paths
    idx_timeline = paths.index("/api/executions/timeline")
    for generic in ("/api/executions", "/api/executions/{execution_id}"):
        if generic in paths:
            assert idx_timeline < paths.index(generic), (
                f"{generic} is registered before /timeline — the literal would "
                "be captured as a path param"
            )


# ---------------------------------------------------------------------------
# The DB layer, against a real SQLite table.
# ---------------------------------------------------------------------------


def _seed(db_path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE schedule_executions (
            id TEXT PRIMARY KEY, schedule_id TEXT, agent_name TEXT, status TEXT,
            started_at TEXT, message TEXT, triggered_by TEXT,
            cost REAL, context_used INTEGER
        )
        """
    )
    conn.executemany(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, message, triggered_by, cost, context_used) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """Point the engine at a temp SQLite file holding a known fleet."""
    try:
        from db.schedules import stats as stats_mod
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    now = datetime.now(timezone.utc)
    db_path = tmp_path / "t.db"
    _seed(db_path, [
        ("e1", "s", "a1", "success", _utc_iso(now - timedelta(hours=1)), "m", "schedule", 0.10, 100),
        ("e2", "s", "a1", "failed",  _utc_iso(now - timedelta(hours=2)), "m", "manual",   0.20, 200),
        ("e3", "s", "a2", "success", _utc_iso(now - timedelta(hours=3)), "m", "schedule", 0.30, 300),
        ("e4", "s", "a2", "error",   _utc_iso(now - timedelta(hours=4)), "m", "webhook",  None, None),
        # Outside a 24h window.
        # 10 days: outside a 24h window, INSIDE the 720h one. 40 days was my
        # own arithmetic slip — 960h is outside both, so the test proved nothing.
        ("e5", "s", "a1", "success", _utc_iso(now - timedelta(days=10)), "m", "schedule", 9.99, 999),
    ])

    from sqlalchemy import create_engine
    engine = create_engine(f"sqlite:///{db_path}")
    monkeypatch.setattr(stats_mod, "get_engine", lambda: engine)
    return stats_mod


def test_db_groups_by_agent_and_scopes(seeded_db):
    mixin = seeded_db.ScheduleStatsMixin
    rows = {r["bucket"]: r for r in
            mixin.get_fleet_execution_timeline(object(), ["a1"], group_by="agent", hours=24)}
    assert set(rows) == {"a1"}, "another agent's executions leaked into the result"
    assert rows["a1"]["total"] == 2
    assert rows["a1"]["failed"] == 1
    assert rows["a1"]["success"] == 1


def test_db_counts_error_as_failed(seeded_db):
    """`failed` and `error` are both terminal failures — `/stats` treats them
    identically and a chart that disagreed would contradict the stat card
    directly above it."""
    mixin = seeded_db.ScheduleStatsMixin
    rows = {r["bucket"]: r for r in
            mixin.get_fleet_execution_timeline(object(), None, group_by="agent", hours=24)}
    assert rows["a2"]["failed"] == 1


def test_db_treats_null_cost_and_context_as_zero(seeded_db):
    """A NULL must not poison the SUM — `e4` has neither."""
    mixin = seeded_db.ScheduleStatsMixin
    rows = {r["bucket"]: r for r in
            mixin.get_fleet_execution_timeline(object(), None, group_by="agent", hours=24)}
    assert rows["a2"]["cost"] == 0.3
    assert rows["a2"]["context_used"] == 300


def test_db_respects_the_window(seeded_db):
    """The 40-day-old row must not appear in a 24h query, and must in 720h."""
    mixin = seeded_db.ScheduleStatsMixin
    short = mixin.get_fleet_execution_timeline(object(), None, group_by="agent", hours=24)
    assert sum(r["total"] for r in short) == 4
    long = mixin.get_fleet_execution_timeline(object(), None, group_by="agent", hours=720)
    assert sum(r["total"] for r in long) == 5


def test_db_hour_buckets_are_utc_iso_prefixes(seeded_db):
    """Buckets slice the stored ISO-Z string (Invariant #16) rather than calling
    a date function, so they are the same UTC the row was written with — and
    dialect-agnostic across SQLite and PostgreSQL."""
    mixin = seeded_db.ScheduleStatsMixin
    rows = mixin.get_fleet_execution_timeline(object(), None, group_by="hour", hours=24)
    for r in rows:
        assert len(r["bucket"]) == 13 and r["bucket"][10] == "T"


def test_db_rejects_an_unvalidated_group_by(seeded_db):
    """Defence in depth behind the router's 422: the key is interpolated into
    SQL, so an unknown value must raise rather than reach the query."""
    mixin = seeded_db.ScheduleStatsMixin
    with pytest.raises(ValueError):
        mixin.get_fleet_execution_timeline(
            object(), None, group_by="status; DROP TABLE schedule_executions", hours=24,
        )


# ---------------------------------------------------------------------------
# The token question (an explicit AC).
# ---------------------------------------------------------------------------


def test_the_context_field_is_named_for_what_it_holds():
    """ent#326 requires settling this. `schedule_executions` has NO token
    columns — `output_tokens` lives only on `chat_messages`, which covers chat
    turns rather than fleet executions. So the endpoint reports context-window
    occupancy and says so; #101's tile must be labelled to match. A field called
    `tokens` here would be the mislabel the issue names."""
    from models import ExecutionTimelineBucket

    fields = set(ExecutionTimelineBucket.model_fields)
    assert "context_used" in fields
    assert not (fields & _USAGE_TOKEN_NAMES), (
        "no field may imply token USAGE — the table cannot answer that question"
    )


def test_schedule_executions_still_has_no_token_column():
    """Pins the premise of the decision above. If a token column is ever added
    (the schema-change option ent#326 lists), this fails and the choice gets
    revisited deliberately rather than silently."""
    from db import tables

    cols = set(tables.schedule_executions.c.keys())
    assert "context_used" in cols
    # Matched by NAME, not by substring: `claim_token` (the #1081 pull-lease CAS
    # token) contains "token" and is not a usage measure at all. A substring
    # check here fails on a column that has nothing to do with the question.
    assert not (cols & _USAGE_TOKEN_NAMES), (
        "schedule_executions grew a token-usage column — revisit ent#326's "
        "decision and #101's tile label"
    )


# ---------------------------------------------------------------------------
# Principals.
# ---------------------------------------------------------------------------

from dataclasses import dataclass          # noqa: E402
from typing import Optional as _Opt        # noqa: E402


@dataclass
class _Admin:
    id: int = 1
    username: str = "admin"
    email: _Opt[str] = "admin@example.com"
    role: str = "admin"
    agent_name: _Opt[str] = None
    connector_agent: _Opt[str] = None


@dataclass
class _User:
    id: int = 2
    username: str = "bob"
    email: _Opt[str] = "bob@example.com"
    role: str = "user"
    agent_name: _Opt[str] = None
    connector_agent: _Opt[str] = None
