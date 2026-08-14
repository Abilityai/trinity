"""`GET /api/executions/timeline?split=trigger` — the second dimension (ent#96).

The executions tile stacks 24 hourly columns by trigger bucket. `_VALID_GROUP_BY`
grouped one way at a time, so the tile could have had 24 buckets OR a trigger
breakdown, not both — one call per bucket name being the alternative.

What these pin:

  * the split rows fold to ONE row per interval whose `by_trigger` sums to the
    interval's own total (a stack that does not add up to its bar is the defect
    a second query would invite);
  * per-label `failed` rides alongside `total`, so a stack can show failures
    without hiding them inside the columns;
  * gap-filled intervals carry `{}` rather than a missing key, so a chart never
    has to tell "no runs" apart from "no field";
  * the unsplit response is untouched — ent#326's contract does not move;
  * `split` is validated to a NAMED 422 wherever it is meaningless, on the #326
    rule that an axis the caller did not ask for (or asked for and did not get)
    is a quietly wrong chart.

Pure-function tests over the shaping layer plus router-level validation; no DB,
no Docker.
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

from db.schedules.stats import _shape_execution_timeline
from db.schedules.analytics import _BUCKET_ORDER

pytestmark = pytest.mark.unit


def _row(bucket, split_key, total, failed=0, success=None):
    return {
        "bucket": bucket,
        "split_key": split_key,
        "total": total,
        "failed": failed,
        "success": total - failed if success is None else success,
        "cost": 0.0,
        "context_used": 0,
    }


# ---------------------------------------------------------------------------
# Folding the second dimension
# ---------------------------------------------------------------------------

def test_split_rows_fold_to_one_row_per_interval():
    rows = [
        _row("2026-08-14T09", "schedule", 10),
        _row("2026-08-14T09", "mcp", 4),
        _row("2026-08-14T10", "schedule", 2),
    ]
    out = _shape_execution_timeline(rows, group_by="hour", hours=24, split="trigger")
    by_bucket = {r["bucket"]: r for r in out}
    assert by_bucket["2026-08-14T09"]["total"] == 14
    assert by_bucket["2026-08-14T09"]["by_trigger"] == {
        "Scheduled": {"total": 10, "failed": 0},
        "MCP": {"total": 4, "failed": 0},
    }


def test_the_breakdown_always_sums_to_its_own_column():
    rows = [
        _row("2026-08-14T09", "schedule", 10, failed=2),
        _row("2026-08-14T09", "chat", 5, failed=1),
        _row("2026-08-14T09", "telegram", 1),
    ]
    out = _shape_execution_timeline(rows, group_by="hour", hours=24, split="trigger")
    hour = next(r for r in out if r["bucket"] == "2026-08-14T09")
    assert sum(e["total"] for e in hour["by_trigger"].values()) == hour["total"] == 16
    assert sum(e["failed"] for e in hour["by_trigger"].values()) == hour["failed"] == 3


def test_failures_are_carried_per_label_not_only_in_the_column_total():
    rows = [
        _row("2026-08-14T09", "schedule", 10, failed=2),
        _row("2026-08-14T09", "mcp", 4, failed=0),
    ]
    out = _shape_execution_timeline(rows, group_by="hour", hours=24, split="trigger")
    hour = next(r for r in out if r["bucket"] == "2026-08-14T09")
    assert hour["by_trigger"]["Scheduled"]["failed"] == 2
    assert hour["by_trigger"]["MCP"]["failed"] == 0


def test_an_unmapped_trigger_lands_in_Other_rather_than_vanishing():
    """The reason the fold is Python and not a SQL CASE: a trigger added to the
    platform but not to `_TRIGGER_BUCKETS` must still be visible."""
    rows = [_row("2026-08-14T09", "brand_new_trigger", 3)]
    out = _shape_execution_timeline(rows, group_by="hour", hours=24, split="trigger")
    hour = next(r for r in out if r["bucket"] == "2026-08-14T09")
    assert hour["by_trigger"] == {"Other": {"total": 3, "failed": 0}}
    assert hour["total"] == 3


def test_two_raw_triggers_in_one_bucket_merge_into_that_bucket():
    """`manual` and `chat` are both `Chat/Tasks` — the fold must add, not
    overwrite, or half the column disappears."""
    rows = [
        _row("2026-08-14T09", "manual", 3),
        _row("2026-08-14T09", "chat", 4),
    ]
    out = _shape_execution_timeline(rows, group_by="hour", hours=24, split="trigger")
    hour = next(r for r in out if r["bucket"] == "2026-08-14T09")
    assert hour["by_trigger"]["Chat/Tasks"] == {"total": 7, "failed": 0}


# ---------------------------------------------------------------------------
# Gap filling under a split
# ---------------------------------------------------------------------------

def test_gap_filled_intervals_carry_an_empty_breakdown_not_a_missing_key():
    rows = [_row("2026-08-14T09", "schedule", 1)]
    out = _shape_execution_timeline(rows, group_by="hour", hours=6, split="trigger")
    assert len(out) >= 6
    for row in out:
        assert "by_trigger" in row, row["bucket"]
        assert isinstance(row["by_trigger"], dict)
    empty = [r for r in out if r["total"] == 0]
    assert empty, "the window should contain at least one quiet hour"
    assert all(r["by_trigger"] == {} for r in empty)


def test_the_axis_is_still_continuous_under_a_split():
    out = _shape_execution_timeline([], group_by="hour", hours=6, split="trigger")
    assert [r["total"] for r in out] == [0] * len(out)
    assert len(out) >= 6


# ---------------------------------------------------------------------------
# The unsplit contract does not move (ent#326)
# ---------------------------------------------------------------------------

def test_without_a_split_no_by_trigger_key_appears():
    rows = [{"bucket": "2026-08-14T09", "total": 3, "failed": 0, "success": 3,
             "cost": 0.0, "context_used": 0}]
    out = _shape_execution_timeline(rows, group_by="hour", hours=6)
    assert all("by_trigger" not in r for r in out)


def test_group_by_trigger_is_unchanged_and_orders_Other_last():
    rows = [
        {"bucket": "schedule", "total": 5, "failed": 0, "success": 5, "cost": 0.0, "context_used": 0},
        {"bucket": "weird", "total": 1, "failed": 0, "success": 1, "cost": 0.0, "context_used": 0},
        {"bucket": "mcp", "total": 2, "failed": 0, "success": 2, "cost": 0.0, "context_used": 0},
    ]
    out = _shape_execution_timeline(rows, group_by="trigger", hours=24)
    names = [r["bucket"] for r in out]
    assert names[-1] == "Other"
    assert names == sorted(names, key=lambda n: _BUCKET_ORDER.index(n))


# ---------------------------------------------------------------------------
# Router validation — a wrong axis is refused by name
# ---------------------------------------------------------------------------

def _user():
    return types.SimpleNamespace(id=1, username="admin", role="admin",
                                 agent_name=None, email="a@b.c")


async def _call(**kwargs):
    import routers.executions as ex

    params = {"group_by": "hour", "hours": 24, "agent": None, "split": None,
              "current_user": _user()}
    params.update(kwargs)
    return await ex.get_fleet_execution_timeline(**params)


@pytest.mark.asyncio
@pytest.mark.parametrize("group_by", ["trigger", "agent"])
async def test_a_split_over_a_categorical_grouping_is_a_named_422(group_by):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _call(group_by=group_by, split="trigger")
    assert exc.value.status_code == 422
    assert "needs a time grouping" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_an_unknown_split_is_a_named_422():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _call(split="nonsense")
    assert exc.value.status_code == 422
    assert "Unsupported split" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_the_stack_order_is_served_only_with_a_split():
    """The tile must not hold its own copy of the order (AC1), so the response
    carries it — and only when it means something."""
    db_mock = MagicMock()
    db_mock.get_fleet_execution_timeline.return_value = []
    db_mock.shape_execution_timeline.return_value = []
    db_mock.trigger_bucket_order.return_value = list(_BUCKET_ORDER)
    with patch("routers.executions.db", db_mock), \
         patch("routers.executions.accessible_agent_names", return_value=None), \
         patch("routers.executions.narrow_to_agent", return_value=None):
        split = await _call(split="trigger")
        plain = await _call(split=None)

    assert split.split == "trigger"
    assert split.trigger_order == list(_BUCKET_ORDER)
    assert plain.split is None
    assert plain.trigger_order is None


@pytest.mark.asyncio
async def test_the_split_reaches_both_db_calls():
    """A split dropped between the query and the shaping would return raw
    `(bucket, trigger)` rows to the response model — one row per trigger per
    hour, i.e. a chart with 24 x N columns."""
    db_mock = MagicMock()
    db_mock.get_fleet_execution_timeline.return_value = []
    db_mock.shape_execution_timeline.return_value = []
    db_mock.trigger_bucket_order.return_value = list(_BUCKET_ORDER)
    with patch("routers.executions.db", db_mock), \
         patch("routers.executions.accessible_agent_names", return_value=None), \
         patch("routers.executions.narrow_to_agent", return_value=None):
        await _call(split="trigger")

    assert db_mock.get_fleet_execution_timeline.call_args.kwargs["split"] == "trigger"
    assert db_mock.shape_execution_timeline.call_args.kwargs["split"] == "trigger"
