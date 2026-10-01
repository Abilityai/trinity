"""Restatement: a corrected value at the same identity updates the row
(trinity-enterprise#729, ruling R45).

Real rows through the real `database.db` facade on `db_backend` (SQLite, and
PostgreSQL when `TEST_POSTGRES_URL` is set). The upsert's `IS DISTINCT FROM`
and its `RETURNING revision` count are dialect-rendered, so every assertion
that only PostgreSQL could fail carries `requires_postgres` — CI's PostgreSQL
tier selects by that marker, and a `db_backend` test without it never runs
there (learning 2026-09-15).

The read half is proved through the real `read_agent_metrics` and
`latest_by_metric`, not through the store beneath them: "no read-path change"
is a claim about what a consumer sees, so the parity test compares what two
consumers see.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
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

from sqlalchemy import event, select, text  # noqa: E402

from database import db  # noqa: E402
from db.engine import get_engine  # noqa: E402
from db.tables import metric_points  # noqa: E402
from services import (  # noqa: E402
    metric_points_service as svc,
    metric_read_service,
    metric_registry,
    template_metrics,
)

AGENT = "restate-agent"
TS = "2026-09-22T12:00:00.000000Z"
FIRST = "2026-09-22T12:05:00.000000Z"
LATER = "2026-09-23T08:00:00.000000Z"


def _row(metric="cycles", ts=TS, value=1.0, dims=None, text_value=None,
         execution_id=None, created_at=FIRST):
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
            .order_by(metric_points.c.ts, metric_points.c.idempotency_key)
        ).mappings()]


def _counts(result):
    return (result.recorded, result.deduplicated, result.corrected)


# ---------------------------------------------------------------------------
# The upsert
# ---------------------------------------------------------------------------

@pytest.mark.requires_postgres
def test_a_different_value_restates_the_row_in_place(db_backend):
    db.insert_metric_points(AGENT, [_row(value=1.0, created_at=FIRST)])

    result = db.insert_metric_points(
        AGENT, [_row(value=2.5, created_at=LATER)])

    assert _counts(result) == (0, 0, 1)
    rows = _stored()
    assert len(rows) == 1, "one identity is one row, before and after"
    row = rows[0]
    assert row["value_numeric"] == 2.5
    assert row["revision"] == 1
    assert row["recorded_at"] == LATER, "recorded_at is the correcting write's clock"
    assert row["ts"] == TS, "ts is the period the value describes; it never moves"
    assert row["created_at"] == FIRST, "created_at is the first write; the cap counts it"


@pytest.mark.requires_postgres
def test_an_identical_value_is_a_duplicate_and_writes_nothing(db_backend):
    db.insert_metric_points(
        AGENT, [_row(value=1.0, execution_id="exec-1", created_at=FIRST)])

    result = db.insert_metric_points(
        AGENT, [_row(value=1.0, execution_id="exec-2", created_at=LATER)])

    assert _counts(result) == (0, 1, 0)
    row = _stored()[0]
    assert row["revision"] == 0
    assert row["recorded_at"] == FIRST, "a duplicate is not a write"
    assert row["execution_id"] == "exec-1", "a duplicate does not take provenance"


def test_a_fresh_row_is_stamped_revision_zero_and_its_write_time(db_backend):
    result = db.insert_metric_points(AGENT, [_row(created_at=FIRST)])

    assert _counts(result) == (1, 0, 0)
    row = _stored()[0]
    assert row["revision"] == 0
    assert row["recorded_at"] == FIRST


def test_a_row_dict_cannot_smuggle_its_own_revision_or_write_time(db_backend):
    """The store stamps both, exactly as it stamps `agent_name`."""
    db.insert_metric_points(AGENT, [dict(
        _row(created_at=FIRST), revision=41, recorded_at="1999-01-01T00:00:00Z")])

    row = _stored()[0]
    assert row["revision"] == 0 and row["recorded_at"] == FIRST


@pytest.mark.requires_postgres
def test_each_correction_bumps_the_revision_again(db_backend):
    db.insert_metric_points(AGENT, [_row(value=1.0)])
    db.insert_metric_points(AGENT, [_row(value=2.0)])
    db.insert_metric_points(AGENT, [_row(value=3.0)])

    row = _stored()[0]
    assert (row["value_numeric"], row["revision"]) == (3.0, 2)


@pytest.mark.requires_postgres
def test_a_mixed_batch_counts_each_outcome_separately(db_backend):
    """Asymmetric on purpose (1 new / 2 identical / 3 corrected): a batch of
    one of each cannot tell a swapped field from a correct one."""
    stored = [_row(ts=f"2026-09-22T1{h}:00:00.000000Z", value=float(h))
              for h in range(5)]
    db.insert_metric_points(AGENT, stored)

    batch = [
        _row(ts="2026-09-22T19:00:00.000000Z", value=9.0),          # new
        _row(ts="2026-09-22T10:00:00.000000Z", value=0.0),          # identical
        _row(ts="2026-09-22T11:00:00.000000Z", value=1.0),          # identical
        _row(ts="2026-09-22T12:00:00.000000Z", value=20.0),         # corrected
        _row(ts="2026-09-22T13:00:00.000000Z", value=30.0),         # corrected
        _row(ts="2026-09-22T14:00:00.000000Z", value=40.0),         # corrected
    ]
    result = db.insert_metric_points(AGENT, batch)

    assert _counts(result) == (1, 2, 3)
    by_ts = {r["ts"][11:13]: r for r in _stored()}
    assert [by_ts[h]["value_numeric"] for h in ("10", "11", "12", "13", "14", "19")] \
        == [0.0, 1.0, 20.0, 30.0, 40.0, 9.0]
    assert [by_ts[h]["revision"] for h in ("10", "11", "12", "13", "14", "19")] \
        == [0, 0, 1, 1, 1, 0]


@pytest.mark.requires_postgres
def test_a_status_label_correction_restates_and_a_repeat_does_not(db_backend):
    """A status row keeps its value in `value_text` with `value_numeric` NULL:
    the NULL column must not turn an identical label into a "difference", nor
    hide a changed one."""
    db.insert_metric_points(
        AGENT, [_row(metric="mood", value=None, text_value="ok")])

    same = db.insert_metric_points(
        AGENT, [_row(metric="mood", value=None, text_value="ok")])
    changed = db.insert_metric_points(
        AGENT, [_row(metric="mood", value=None, text_value="degraded")])

    assert _counts(same) == (0, 1, 0)
    assert _counts(changed) == (0, 0, 1)
    row = _stored()[0]
    assert (row["value_text"], row["value_numeric"], row["revision"]) \
        == ("degraded", None, 1)


@pytest.mark.requires_postgres
def test_a_value_that_moves_between_the_two_columns_is_still_a_change(db_backend):
    """Why the comparison is `IS DISTINCT FROM` and not `!=`. When the value
    moves from one column to the other, BOTH plain comparisons are `x != NULL`
    = NULL, the `OR` is NULL, and a real change would be counted a duplicate.
    The validator never produces this today (the stored type decides), so this
    pins the store's own contract rather than a reachable route input."""
    db.insert_metric_points(AGENT, [_row(value=1.0, text_value=None)])

    result = db.insert_metric_points(
        AGENT, [_row(value=None, text_value="ok")])

    assert _counts(result) == (0, 0, 1)
    row = _stored()[0]
    assert (row["value_numeric"], row["value_text"]) == (None, "ok")


@pytest.mark.requires_postgres
def test_execution_id_follows_the_write_that_produced_the_current_value(db_backend):
    db.insert_metric_points(AGENT, [_row(value=1.0, execution_id="exec-1")])

    db.insert_metric_points(AGENT, [_row(value=2.0, execution_id="exec-2")])
    assert _stored()[0]["execution_id"] == "exec-2"

    # A correction whose execution could not be confirmed carries none — the
    # current value was NOT written by exec-2, so it must not be credited.
    db.insert_metric_points(AGENT, [_row(value=3.0, execution_id=None)])
    assert _stored()[0]["execution_id"] is None


def test_dims_keep_the_first_writes_key_order(db_backend):
    """Same identity ⇒ same canonical dims; the stored mapping is not rewritten."""
    db.insert_metric_points(
        AGENT, [_row(dims={"region": "eu", "tier": "pro"}, value=1.0)])
    db.insert_metric_points(
        AGENT, [_row(dims={"tier": "pro", "region": "eu"}, value=2.0)])

    rows = _stored()
    assert len(rows) == 1
    assert rows[0]["dims"] == {"region": "eu", "tier": "pro"}
    assert rows[0]["value_numeric"] == 2.0


def test_two_rows_with_one_identity_in_one_call_are_refused(db_backend):
    """PostgreSQL raises a cardinality violation on this and SQLite silently
    applies both — the store refuses it so the dialects cannot disagree. The
    route never reaches it (`duplicate_in_batch`), which is the point."""
    with pytest.raises(ValueError, match="identity"):
        db.insert_metric_points(AGENT, [_row(value=1.0), _row(value=2.0)])
    assert _stored() == []


def test_rows_are_written_in_ts_then_key_order(db_backend):
    """`DO UPDATE` locks every conflicting row, even when its WHERE is false.
    Two overlapping batches that lock in different orders can deadlock on
    PostgreSQL; one canonical order makes that unreachable. Observed at the
    driver, where the order is the one the database receives."""
    if db_backend != "sqlite":
        pytest.skip("the order is set in Python; one dialect proves it")
    batch = [_row(ts=f"2026-09-22T1{h}:00:00.000000Z") for h in (4, 1, 3, 0, 2)]
    expected = sorted(r["ts"] for r in batch)
    seen = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO metric_points" in statement:
            seen.extend(p for p in parameters if p in expected)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", _capture)
    try:
        db.insert_metric_points(AGENT, batch)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert seen == expected


def test_the_daily_count_is_not_spent_by_a_correction(db_backend):
    """Ruled 2026-10-01: the cap counts rows CREATED, and a correction keeps
    its row's `created_at` — so restating a point never adds to "used today"."""
    day = "2026-09-22T00:00:00.000000Z"
    db.insert_metric_points(AGENT, [_row(value=1.0, created_at=FIRST)])
    db.insert_metric_points(
        AGENT, [_row(value=2.0, created_at="2026-09-22T23:00:00.000000Z")])

    assert db.count_metric_points_today(AGENT, day, 10) == 1


# ---------------------------------------------------------------------------
# Migration: existing rows, and code from before this change
# ---------------------------------------------------------------------------

def _load_migrations():
    spec = importlib.util.spec_from_file_location(
        "_migrations_ent729", _BACKEND / "db" / "migrations.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rows_written_before_the_columns_existed_read_revision_zero(tmp_path):
    """The SQLite track, on the table shape ent#478 built: existing rows read
    `revision 0` and a NULL `recorded_at` (written before ent#729 — its write
    time is `created_at`). No backfill rewrites the table."""
    migrations = _load_migrations()
    conn = sqlite3.connect(tmp_path / "old.db")
    cur = conn.cursor()
    migrations._migrate_metric_points_table(cur, conn)
    cur.execute(
        "INSERT INTO metric_points (agent_name, metric, ts, idempotency_key, "
        "value_numeric, created_at) VALUES ('a', 'm', ?, 'k', 1.0, ?)",
        (TS, FIRST))
    conn.commit()

    migrations._migrate_metric_points_restatement(cur, conn)
    migrations._migrate_metric_points_restatement(cur, conn)  # idempotent

    cur.execute("PRAGMA table_info(metric_points)")
    columns = {r[1]: r for r in cur.fetchall()}
    assert columns["revision"][2].upper() == "BIGINT"
    assert columns["revision"][3] == 1, "revision is NOT NULL"
    assert columns["recorded_at"][3] == 0, "recorded_at stays nullable"
    assert cur.execute(
        "SELECT revision, recorded_at FROM metric_points").fetchall() == [(0, None)]
    conn.close()


def test_code_from_before_this_change_can_still_insert(db_backend):
    """Rollback safety: the eyeball runs this migration on a live dev DB, and a
    checkout switched back to `dev` must keep recording. An INSERT naming
    neither new column — the pre-ent#729 writer — succeeds and reads
    `revision 0`; the store then corrects that row normally."""
    with get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO metric_points (agent_name, metric, ts, "
            "idempotency_key, value_numeric, created_at) "
            "VALUES (:a, 'cycles', :ts, :k, 1.0, :c)"),
            {"a": AGENT, "ts": TS, "k": svc.point_identity("cycles", TS, None),
             "c": FIRST})

    row = _stored()[0]
    assert (row["revision"], row["recorded_at"]) == (0, None)

    result = db.insert_metric_points(AGENT, [_row(value=5.0, created_at=LATER)])
    assert _counts(result) == (0, 0, 1)
    assert (_stored()[0]["revision"], _stored()[0]["recorded_at"]) == (1, LATER)


@pytest.mark.requires_postgres
def test_the_alembic_revision_adds_the_columns_over_existing_rows_on_postgres(
        monkeypatch):
    """The ONLY path an existing PostgreSQL install takes. A fresh database is
    built from `db/schema.py` (which already declares the columns), so the
    upgrade path is reproduced explicitly: bring the schema to the previous
    head, put the table back in its ent#478 shape with a row in it, then
    upgrade to head and read the row back."""
    pg_url = os.environ.get("TEST_POSTGRES_URL")
    if not pg_url:
        pytest.skip("TEST_POSTGRES_URL not set")

    from alembic import command

    from db.alembic_runner import _config
    from db.engine import dispose_engines
    from db_harness import _reset_postgres

    monkeypatch.setenv("DATABASE_URL", pg_url)
    dispose_engines()
    try:
        _reset_postgres()
        cfg = _config()
        command.upgrade(cfg, "0085_ent720_email_identity")
        with get_engine().begin() as conn:
            conn.execute(text(
                "ALTER TABLE metric_points DROP COLUMN IF EXISTS revision"))
            conn.execute(text(
                "ALTER TABLE metric_points DROP COLUMN IF EXISTS recorded_at"))
            conn.execute(text(
                "INSERT INTO metric_points (agent_name, metric, ts, "
                "idempotency_key, value_numeric, created_at) "
                "VALUES ('a', 'm', :ts, 'k', 1.0, :c)"), {"ts": TS, "c": FIRST})

        command.upgrade(cfg, "head")

        with get_engine().connect() as conn:
            got = conn.execute(select(
                metric_points.c.revision, metric_points.c.recorded_at)).all()
            data_type = conn.execute(text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'metric_points' AND column_name = 'revision'"
            )).scalar_one()
        assert [tuple(r) for r in got] == [(0, None)]
        assert data_type == "bigint"
    finally:
        dispose_engines()


# ---------------------------------------------------------------------------
# The read: freshness unmoved, and no read-path change
# ---------------------------------------------------------------------------

def _declare(agent=AGENT):
    declared = template_metrics.normalize_declared_metrics([
        {"name": "revenue", "type": "gauge", "cadence": "1h",
         "aggregation": "sum", "dimensions": ["region"]},
        {"name": "mood", "type": "status",
         "values": [{"value": "ok"}, {"value": "degraded"}]},
    ])
    metric_registry.reconcile_declared_metrics(agent, declared, source="create")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _record(agent, points, now):
    """The real write path below HTTP: the service validates, the store writes."""
    definitions = db.list_metric_definitions(agent, include_retired=True)
    rows, errors = svc.validate_batch(definitions, points, now=now)
    assert errors == []
    return db.insert_metric_points(agent, rows)


def _revenue(read):
    return next(m for m in read["metrics"] if m["name"] == "revenue")


def test_a_correction_does_not_freshen_a_stale_series(db_backend):
    """Record, age, correct, read: the value moves, `last_point_at` and
    `stale` do not. A restated W39 must not look freshly measured."""
    _declare()
    t0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3)
    point_ts = _iso(t0)

    _record(AGENT, [{"metric": "revenue", "value": 100, "ts": point_ts,
                     "dims": {"region": "eu"}}], now=t0)
    fresh = _revenue(metric_read_service.read_agent_metrics(
        AGENT, now=t0 + timedelta(minutes=30)))
    assert fresh["stale"] is False

    aged_now = t0 + timedelta(hours=3)            # > 2 × the 1h cadence
    before = _revenue(metric_read_service.read_agent_metrics(AGENT, now=aged_now))
    assert before["stale"] is True

    result = _record(AGENT, [{"metric": "revenue", "value": 125, "ts": point_ts,
                              "dims": {"region": "eu"}}], now=aged_now)
    assert _counts(result) == (0, 0, 1)

    after = _revenue(metric_read_service.read_agent_metrics(AGENT, now=aged_now))
    assert after["latest"]["value"] == 125
    assert after["last_point_at"] == before["last_point_at"] == point_ts
    assert (after["stale"], after["freshness"], after["stale_after"]) \
        == (before["stale"], before["freshness"], before["stale_after"])


def _strip_agent(value, agent):
    """The same read for two agents differs only by the name it echoes."""
    if isinstance(value, dict):
        return {k: _strip_agent(v, agent) for k, v in value.items()
                if k != "agent_name"}
    if isinstance(value, list):
        return [_strip_agent(v, agent) for v in value]
    return value


def _keys(value, out=None):
    out = set() if out is None else out
    if isinstance(value, dict):
        out.update(value)
        for v in value.values():
            _keys(v, out)
    elif isinstance(value, list):
        for v in value:
            _keys(v, out)
    return out


def test_a_corrected_store_reads_exactly_like_a_born_correct_one(db_backend):
    """The parity pin for "no read-path change": an agent whose points were
    recorded and then corrected reads — through `get_metrics`' composition and
    through the ent#666 objective join's `latest_by_metric` — exactly like an
    agent that recorded the final values the first time."""
    corrected, born = "restate-corrected", "restate-born"
    _declare(corrected)
    _declare(born)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    ts_a, ts_b = _iso(now - timedelta(hours=2)), _iso(now - timedelta(hours=1))

    final = [
        {"metric": "revenue", "value": 10, "ts": ts_a, "dims": {"region": "eu"}},
        {"metric": "revenue", "value": 7, "ts": ts_b, "dims": {"region": "eu"}},
        {"metric": "revenue", "value": 3, "ts": ts_b, "dims": {"region": "us"}},
        {"metric": "mood", "value": "degraded", "ts": ts_b},
    ]
    first = [dict(p) for p in final]
    first[1]["value"], first[2]["value"], first[3]["value"] = 70, 30, "ok"

    _record(corrected, first, now=now)
    assert _counts(_record(corrected, final, now=now)) == (0, 1, 3)
    _record(born, final, now=now)

    for read in (
        lambda a: metric_read_service.read_agent_metrics(a, now=now),
        lambda a: metric_read_service.read_agent_metrics(
            a, metric="revenue", now=now),
        lambda a: metric_read_service.latest_by_metric(a, now=now),
    ):
        got_corrected = read(corrected)
        assert _strip_agent(got_corrected, corrected) \
            == _strip_agent(read(born), born)
        assert not _keys(got_corrected) & {"revision", "recorded_at"}, \
            "a write-side column leaked onto a read"


def test_the_store_reads_select_no_write_side_column(db_backend):
    db.insert_metric_points(AGENT, [_row(value=1.0)])
    db.insert_metric_points(AGENT, [_row(value=2.0)])

    since = "2026-01-01T00:00:00.000000Z"
    for rows in (db.latest_metric_points(AGENT, ["cycles"]),
                 db.metric_series_points(AGENT, "cycles", since)):
        assert rows and rows[0]["value_numeric"] == 2.0
        assert not set(rows[0]) & {"revision", "recorded_at", "created_at"}
