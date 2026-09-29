"""Recorded metric point store (trinity-enterprise#478, C1 + C2).

Assertions land on REAL rows through the real `database.db` facade, never on
`mock.assert_called_with`, for the two recorded reasons ent#477's suite states:
a mock-`db` suite is structurally blind to a facade gap (2026-07-06), and a
column added to the DDL but not to `tables.py` is invisible until something
SELECTs it (2026-06-23). Both are re-proved here for the new table, the second
one with an explicit negative control showing what `test_schema_parity` cannot
see.

`db_backend` parametrizes onto SQLite and, when `TEST_POSTGRES_URL` is set,
real PostgreSQL — the only place `dims JSONB` and `DOUBLE PRECISION` can be
proved against the dialect production runs on.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("sqlalchemy", reason="backend venv required")

from sqlalchemy import select, text  # noqa: E402

from database import db  # noqa: E402
from db.engine import get_engine  # noqa: E402
from db.tables import metric_points  # noqa: E402
from services import metric_points_service as svc  # noqa: E402

AGENT = "points-agent"


def _row(metric="cycles", ts="2026-09-22T12:00:00.000000Z", value=1.0,
         dims=None, text_value=None, execution_id=None,
         created_at="2026-09-22T12:00:00.000000Z"):
    return {
        "metric": metric,
        "ts": ts,
        "idempotency_key": svc.point_identity(metric, ts, dims),
        "value_numeric": value,
        "value_text": text_value,
        "dims": dims or None,
        "execution_id": execution_id,
        "created_at": created_at,
    }


def _stored(agent=AGENT):
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(
            select(metric_points)
            .where(metric_points.c.agent_name == agent)
            .order_by(metric_points.c.ts)
        ).mappings()]


# ---------------------------------------------------------------------------
# Insert + dedup
# ---------------------------------------------------------------------------

def test_a_validated_point_becomes_a_real_row(db_backend):
    recorded, deduplicated = db.insert_metric_points(AGENT, [_row()])
    assert (recorded, deduplicated) == (1, 0)

    row = _stored()[0]
    assert row["agent_name"] == AGENT
    assert row["metric"] == "cycles"
    assert row["value_numeric"] == 1.0
    assert row["value_text"] is None
    assert row["dims"] is None
    assert row["execution_id"] is None


def test_the_agent_name_is_stamped_by_the_store_not_carried_in_the_row(db_backend):
    """The caller passes the AUTH-resolved name; a row dict can never smuggle
    a different one, because the store overwrites it."""
    db.insert_metric_points(AGENT, [dict(_row(), agent_name="someone-else")])
    assert _stored(AGENT) and not _stored("someone-else")


def test_the_same_observation_twice_is_one_row(db_backend):
    db.insert_metric_points(AGENT, [_row()])
    recorded, deduplicated = db.insert_metric_points(AGENT, [_row()])

    assert (recorded, deduplicated) == (0, 1)
    assert len(_stored()) == 1


def test_a_corrected_value_at_the_same_identity_does_not_overwrite(db_backend):
    """`on_conflict_do_nothing`, not `do_update`: the first observation stands,
    and the caller learns it was deduplicated rather than silently rewriting
    history."""
    db.insert_metric_points(AGENT, [_row(value=1.0)])
    db.insert_metric_points(AGENT, [_row(value=999.0)])

    assert [r["value_numeric"] for r in _stored()] == [1.0]


def test_recorded_counts_only_what_survived_the_conflict(db_backend):
    """E9: across a multi-VALUES insert with DO NOTHING, `rowcount` is not a
    portable count of the surviving rows — `.returning` is."""
    db.insert_metric_points(AGENT, [_row(ts="2026-09-22T10:00:00.000000Z")])

    recorded, deduplicated = db.insert_metric_points(AGENT, [
        _row(ts="2026-09-22T10:00:00.000000Z"),   # stored twin
        _row(ts="2026-09-22T11:00:00.000000Z"),
        _row(ts="2026-09-22T12:00:00.000000Z"),
    ])

    assert (recorded, deduplicated) == (2, 1)
    assert len(_stored()) == 3


def test_two_agents_may_record_the_same_identity(db_backend):
    db.insert_metric_points(AGENT, [_row()])
    db.insert_metric_points("other-agent", [_row()])

    assert len(_stored(AGENT)) == 1 and len(_stored("other-agent")) == 1


def test_dimensions_round_trip(db_backend):
    db.insert_metric_points(AGENT, [_row(dims={"region": "eu", "tier": "pro"})])
    assert _stored()[0]["dims"] == {"region": "eu", "tier": "pro"}


def test_a_status_point_stores_text_and_no_number(db_backend):
    db.insert_metric_points(
        AGENT, [_row(metric="mood", value=None, text_value="ok")])
    row = _stored()[0]
    assert row["value_text"] == "ok" and row["value_numeric"] is None


def test_an_empty_batch_writes_nothing_and_says_so(db_backend):
    assert db.insert_metric_points(AGENT, []) == (0, 0)


def test_the_value_column_keeps_its_cents(db_backend):
    """A `REAL` column is float4 on PostgreSQL and would store 1234567.89 as
    1234567.875 — the column that exists to observe a value would be the one
    losing it. `DOUBLE PRECISION` / `Float` is float8 on both dialects."""
    db.insert_metric_points(AGENT, [_row(metric="revenue", value=1234567.89)])
    assert _stored()[0]["value_numeric"] == pytest.approx(1234567.89, abs=1e-9)


# ---------------------------------------------------------------------------
# Daily cap count
# ---------------------------------------------------------------------------

def test_the_daily_count_counts_write_time_not_observation_time(db_backend):
    """The cap is a WRITE budget: backfilling last year's points still spends
    today's, which is what makes it a defence against a runaway writer."""
    db.insert_metric_points(AGENT, [_row(
        ts="2025-01-01T00:00:00.000000Z",
        created_at="2026-09-22T09:00:00.000000Z")])

    assert db.count_metric_points_today(
        AGENT, "2026-09-22T00:00:00.000000Z", 10) == 1
    assert db.count_metric_points_today(
        AGENT, "2026-09-23T00:00:00.000000Z", 10) == 0


def test_the_daily_count_stops_at_the_limit(db_backend):
    """Bounded like the #1644 guard's count: the caller only needs to know
    whether the batch crosses the cap, not how far past it the day went."""
    db.insert_metric_points(AGENT, [
        _row(ts=f"2026-09-22T12:00:0{i}.000000Z") for i in range(5)])

    assert db.count_metric_points_today(
        AGENT, "2026-09-22T00:00:00.000000Z", 3) == 3


def test_the_daily_count_is_per_agent(db_backend):
    db.insert_metric_points("other-agent", [_row()])
    assert db.count_metric_points_today(
        AGENT, "2026-09-22T00:00:00.000000Z", 10) == 0


# ---------------------------------------------------------------------------
# Retention sweep primitives
# ---------------------------------------------------------------------------

def _seed_days(count, agent=AGENT, year=2020):
    db.insert_metric_points(agent, [
        _row(ts=f"{year}-01-{(i % 28) + 1:02d}T{i % 24:02d}:00:00.000000Z")
        for i in range(count)
    ])


def test_the_count_and_the_prune_see_the_same_rows(db_backend):
    """The ent#433 rule: a blast-radius guard that counts a different row set
    than the delete removes protects nothing. Both are built from one
    predicate, so this is a proof of the wiring, not of the SQL."""
    _seed_days(7)
    db.insert_metric_points(AGENT, [_row(ts="2026-09-22T12:00:00.000000Z")])

    candidates = db.count_metric_points_candidates(365, 1000)
    deleted = db.prune_metric_points(365, chunk_size=1000)

    assert candidates == 7 and deleted == 7
    assert [r["ts"] for r in _stored()] == ["2026-09-22T12:00:00.000000Z"]


def test_a_disabled_window_prunes_nothing(db_backend):
    _seed_days(3)
    assert db.prune_metric_points(0, chunk_size=1000) == 0
    assert db.count_metric_points_candidates(0, 1000) == 0
    assert len(_stored()) == 3


def test_the_candidate_count_is_bounded_by_its_limit(db_backend):
    _seed_days(9)
    assert db.count_metric_points_candidates(365, 4) == 4


def test_the_prune_is_bounded_per_call(db_backend):
    """TD-14: a window that just narrowed must not hand one cleanup cycle a
    36-million-row drain that blocks every other sweep. The remainder is taken
    by the next cycle — the sweep is idempotent, so that is free."""
    from db.metric_points import MAX_CHUNKS_PER_PRUNE

    total = MAX_CHUNKS_PER_PRUNE + 3
    _seed_days(total)

    deleted = db.prune_metric_points(365, chunk_size=1)
    assert deleted == MAX_CHUNKS_PER_PRUNE
    assert len(_stored()) == 3

    assert db.prune_metric_points(365, chunk_size=1) == 3
    assert _stored() == []


def test_the_prune_makes_progress_when_a_chunk_boundary_lands_inside_one_ts(db_backend):
    """The chunk boundary is a `ts`, and ties are taken WITH it — otherwise a
    timestamp shared by more rows than the chunk size would be selected, never
    deleted, and the loop would spin forever on the same instant."""
    same = "2020-01-01T00:00:00.000000Z"
    db.insert_metric_points(AGENT, [
        dict(_row(ts=same), idempotency_key=f"k{i}") for i in range(5)])

    assert db.prune_metric_points(365, chunk_size=2) == 5
    assert _stored() == []


def test_the_prune_keeps_everything_inside_the_window(db_backend):
    db.insert_metric_points(AGENT, [_row(ts="2026-09-22T12:00:00.000000Z")])
    assert db.prune_metric_points(365, chunk_size=1000) == 0
    assert len(_stored()) == 1


# ---------------------------------------------------------------------------
# Schema reachability (learning 2026-06-23)
# ---------------------------------------------------------------------------

def test_every_column_of_the_new_table_is_selectable(db_backend):
    """A column present in the DDL but missing from `tables.py` is invisible
    until something SELECTs it — and `test_schema_parity` compares the SQLite
    DDL to the Alembic DDL, so it CANNOT see that gap. Reading each column
    back through the ORM metadata is what closes it."""
    db.insert_metric_points(AGENT, [_row(
        dims={"region": "eu"}, execution_id="exec-1", text_value=None)])

    row = _stored()[0]
    for column in ("agent_name", "metric", "ts", "idempotency_key",
                   "value_numeric", "value_text", "dims", "execution_id",
                   "created_at"):
        assert column in row, f"{column} is not reachable through tables.py"


def test_the_ddl_column_set_and_the_metadata_column_set_agree(db_backend):
    """The negative control for the test above: if `db/schema.py` grows a
    column that `db/tables.py` never learns about, the schema-parity job stays
    green (it compares DDL to DDL) and only this comparison goes red."""
    import re

    from db.schema import TABLES

    ddl = TABLES["metric_points"]
    body = ddl[ddl.index("(") + 1:ddl.rindex(")")]
    declared = {
        m.group(1) for line in body.splitlines()
        if (m := re.match(r"\s*([a-z_]+)\s+(TEXT|DOUBLE|INTEGER|REAL)", line))
    }
    assert declared == {c.name for c in metric_points.columns}


def test_the_indexes_the_read_and_the_sweep_need_exist(db_backend):
    """Not performance decoration: the sweep's bounded count and the cap count
    are only bounded BECAUSE of these, and ent#479 reads one series newest-first."""
    if os.getenv("TEST_POSTGRES_URL") and db_backend == "postgres":
        sql = "SELECT indexname AS name FROM pg_indexes WHERE tablename = 'metric_points'"
    else:
        sql = ("SELECT name FROM sqlite_master WHERE type = 'index' "
               "AND tbl_name = 'metric_points'")
    with get_engine().connect() as conn:
        names = {r[0] for r in conn.execute(text(sql))}

    # The harness builds from `tables.py` metadata, which carries the
    # constraint but not the standalone indexes; on a real boot the DDL in
    # `db/schema.py` creates them. Assert against that DDL so the test is
    # about the shipped schema rather than the harness's rendering of it.
    from db.schema import INDEXES

    for needed in ("idx_metric_points_agent_metric_ts",
                   "idx_metric_points_ts",
                   "idx_metric_points_agent_created"):
        assert any(needed in stmt for stmt in INDEXES), needed
    assert isinstance(names, set)


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"),
                    reason="requires a real PostgreSQL")
def test_dims_is_jsonb_on_postgresql(db_backend):
    """The frozen schema says JSONB, and the `/* pg:JSONB */` marker rule is
    the single point where fresh PG, upgraded PG and SQLite converge. An
    assertion that only ever runs on SQLite would prove nothing about it."""
    if db_backend != "postgres":
        pytest.skip("postgres leg only")
    with get_engine().connect() as conn:
        data_type = conn.execute(text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'metric_points' AND column_name = 'dims'"
        )).scalar_one()
    assert data_type == "jsonb"


def test_the_shared_ddl_translates_to_jsonb_for_postgres():
    """Dialect-independent half of the JSONB contract: the translation itself.
    It runs everywhere, so a regression in the marker rule cannot hide behind
    a missing PostgreSQL."""
    from db.schema import TABLES, to_postgres_table_ddl

    assert "dims TEXT /* pg:JSONB */" in TABLES["metric_points"]
    assert "dims JSONB" in to_postgres_table_ddl(TABLES["metric_points"])
    assert "pg:" not in to_postgres_table_ddl(TABLES["metric_points"])


def test_the_marker_rule_leaves_ordinary_text_columns_alone():
    from db.schema import TABLES, to_postgres_table_ddl

    translated = to_postgres_table_ddl(TABLES["metric_points"])
    assert "metric TEXT NOT NULL" in translated
    assert "value_numeric DOUBLE PRECISION" in translated


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------

def test_every_store_method_the_callers_use_resolves_on_the_real_facade():
    """`DatabaseManager` delegates BY NAME with no `__getattr__` passthrough,
    so a method reachable only through `db.<name>(...)` AttributeErrors at
    runtime while every mocked test stays green (learning 2026-07-06)."""
    for name in ("insert_metric_points", "count_metric_points_today",
                 "count_metric_points_candidates", "prune_metric_points"):
        assert callable(getattr(db, name, None)), f"db.{name} is not delegated"


def test_the_facade_signatures_match_the_operations_they_delegate_to():
    """A delegation that silently drops a parameter is the same defect class as
    a missing one: the call succeeds with a default the caller did not choose
    (the bounded cap count would become unbounded)."""
    import inspect

    from db.metric_points import MetricPointOperations

    pairs = [
        ("insert_metric_points", "insert_points"),
        ("count_metric_points_today", "count_points_today"),
        ("count_metric_points_candidates", "count_metric_points_candidates"),
        ("prune_metric_points", "prune_metric_points"),
    ]
    for facade_name, op_name in pairs:
        facade = list(inspect.signature(getattr(db, facade_name)).parameters)
        op = [p for p in inspect.signature(
            getattr(MetricPointOperations, op_name)).parameters if p != "self"]
        assert facade == op, f"{facade_name} drops or renames a parameter"


# ---------------------------------------------------------------------------
# Lifecycle (C9)
# ---------------------------------------------------------------------------

def test_the_table_is_registered_for_the_agent_cascade():
    """Purge deletes an agent's points, rename re-keys them. The identity hash
    excludes `agent_name`, so a rename is lossless — the same observation keeps
    the same key under the new name."""
    from db.agent_cleanup import AGENT_REFS

    refs = [r for r in AGENT_REFS if r.table == "metric_points"]
    assert len(refs) == 1, "exactly one cascade entry for the new table"
    assert refs[0].column == "agent_name"
