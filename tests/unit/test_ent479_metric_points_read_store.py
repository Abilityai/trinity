"""Metric point READ primitives (trinity-enterprise#479, C1).

Real rows through the real `database.db` facade on `db_backend` (SQLite, and
PostgreSQL when `TEST_POSTGRES_URL` is set) — the only place the dialect-level
promises can be proved: the `(ts DESC, idempotency_key DESC)` tiebreak has to
pick the SAME row on both engines, or "the latest value" is a different number
depending on which database an install runs.
"""

from __future__ import annotations

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

from database import db  # noqa: E402
from services import metric_points_service as svc  # noqa: E402

AGENT = "read-store-agent"


def _row(metric="cycles", ts="2026-09-22T12:00:00.000000Z", value=1.0,
         dims=None, text_value=None):
    return {
        "metric": metric,
        "ts": ts,
        "idempotency_key": svc.point_identity(metric, ts, dims),
        "value_numeric": value,
        "value_text": text_value,
        "dims": dims or None,
        "execution_id": None,
        "created_at": ts,
    }


def _seed(*rows, agent=AGENT):
    db.insert_metric_points(agent, list(rows))


# ---------------------------------------------------------------------------
# latest_metric_points
# ---------------------------------------------------------------------------

def test_the_newest_points_come_back_newest_first(db_backend):
    _seed(
        _row(ts="2026-09-22T10:00:00.000000Z", value=1.0),
        _row(ts="2026-09-22T12:00:00.000000Z", value=3.0),
        _row(ts="2026-09-22T11:00:00.000000Z", value=2.0),
    )
    rows = db.latest_metric_points(AGENT, ["cycles"])
    assert [r["value_numeric"] for r in rows] == [3.0, 2.0, 1.0]


def test_each_metric_is_seeked_independently_to_its_own_depth(db_backend):
    """One seek per declared name, not one partition over the agent's rows: a
    busy metric must not push a quiet one out of the answer."""
    for hour in range(5):
        _seed(_row(metric="busy", ts=f"2026-09-22T1{hour}:00:00.000000Z"))
    _seed(_row(metric="quiet", ts="2026-09-20T09:00:00.000000Z"))

    rows = db.latest_metric_points(AGENT, ["busy", "quiet"], 2)
    by_metric = {}
    for row in rows:
        by_metric.setdefault(row["metric"], []).append(row)
    assert len(by_metric["busy"]) == 2
    assert len(by_metric["quiet"]) == 1


def test_a_tie_on_ts_is_broken_deterministically(db_backend):
    """Two observations of the same metric at the same instant differ only by
    dims, so they share a `ts`. Without the `idempotency_key` tiebreak SQLite
    and PostgreSQL are free to disagree about which is "latest" — the tile
    would show a different region per engine."""
    ts = "2026-09-22T12:00:00.000000Z"
    _seed(_row(ts=ts, value=1.0, dims={"region": "eu"}),
          _row(ts=ts, value=2.0, dims={"region": "us"}))

    first = db.latest_metric_points(AGENT, ["cycles"])
    second = db.latest_metric_points(AGENT, ["cycles"])
    assert [r["idempotency_key"] for r in first] == \
           [r["idempotency_key"] for r in second]
    assert first[0]["idempotency_key"] > first[1]["idempotency_key"]


def test_another_agents_points_are_never_visible(db_backend):
    _seed(_row(value=9.0), agent="somebody-else")
    assert db.latest_metric_points(AGENT, ["cycles"]) == []


def test_an_empty_name_list_reads_nothing(db_backend):
    """The zero-declared case must not degenerate into "every row"."""
    _seed(_row())
    assert db.latest_metric_points(AGENT, []) == []


def test_dims_survive_the_round_trip_as_a_mapping(db_backend):
    _seed(_row(dims={"region": "eu"}))
    assert db.latest_metric_points(AGENT, ["cycles"])[0]["dims"] == {"region": "eu"}


def test_a_status_label_comes_back_in_value_text(db_backend):
    _seed(_row(value=None, text_value="healthy"))
    row = db.latest_metric_points(AGENT, ["cycles"])[0]
    assert row["value_numeric"] is None and row["value_text"] == "healthy"


# ---------------------------------------------------------------------------
# metric_series_points
# ---------------------------------------------------------------------------

def test_the_window_cut_excludes_points_before_since(db_backend):
    _seed(_row(ts="2026-09-20T12:00:00.000000Z", value=1.0),
          _row(ts="2026-09-22T12:00:00.000000Z", value=2.0))
    rows = db.metric_series_points(
        AGENT, "cycles", "2026-09-21T00:00:00.000000Z")
    assert [r["value_numeric"] for r in rows] == [2.0]


def test_until_bounds_the_other_end(db_backend):
    _seed(_row(ts="2026-09-20T12:00:00.000000Z", value=1.0),
          _row(ts="2026-09-22T12:00:00.000000Z", value=2.0))
    rows = db.metric_series_points(
        AGENT, "cycles", "2026-09-01T00:00:00.000000Z",
        "2026-09-21T00:00:00.000000Z")
    assert [r["value_numeric"] for r in rows] == [1.0]


def test_the_series_reads_one_row_past_the_limit_so_truncation_is_knowable(db_backend):
    """`len > limit` is how the service knows to set `series_truncated`
    without a second COUNT over a table the sweep keeps at tens of millions of
    rows."""
    for minute in range(5):
        _seed(_row(ts=f"2026-09-22T12:0{minute}:00.000000Z", value=float(minute)))
    rows = db.metric_series_points(
        AGENT, "cycles", "2026-09-01T00:00:00.000000Z", limit=3)
    assert len(rows) == 4


def test_a_truncated_window_keeps_the_NEWEST_points(db_backend):
    """The direction that matters: `ASC + LIMIT` would answer with last week
    while today is missing."""
    for minute in range(5):
        _seed(_row(ts=f"2026-09-22T12:0{minute}:00.000000Z", value=float(minute)))
    rows = db.metric_series_points(
        AGENT, "cycles", "2026-09-01T00:00:00.000000Z", limit=2)
    assert [r["value_numeric"] for r in rows[:2]] == [4.0, 3.0]


def test_a_zero_limit_reads_nothing_rather_than_everything(db_backend):
    _seed(_row())
    assert db.metric_series_points(
        AGENT, "cycles", "2026-09-01T00:00:00.000000Z", limit=0) == []
