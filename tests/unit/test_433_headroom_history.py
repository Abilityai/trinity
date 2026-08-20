"""Unit tests for subscription headroom history (ent#433).

Persists each #471 headroom probe as a durable row so utilization TRENDS are
answerable, and converts `subscription_rate_limit_events`' hardcoded 24h sweep
into a real retention window.

Several tests here exist because of a SPECIFIC failure mode that no obvious
test catches:

- ``TestRoundTrip`` runs against the REAL migrated DB via ``db_backend``.
  ``tests/unit/test_schema_parity.py`` diffs ``db/schema.py`` against the SQLite
  migration runner and never imports ``db/tables.py`` — so a Column missing
  from the SQLAlchemy Core table stays green in CI and raises at runtime on the
  first probe of the first deploy (docs/memory/learnings.md, 2026-06-23). A
  live ``select(table.c.<col>)`` is the only construct that exercises it.
- ``TestFacadeDelegation`` touches the real ``DatabaseManager``. It has no
  ``__getattr__``, so every accessor needs a hand-written delegation, and any
  suite that mocks the ``database`` module is structurally blind to a missing
  one (learnings, 2026-07-06).
- ``TestDialectPortability`` compiles the bucket query for PostgreSQL. The
  natural shape for this read — ``MAX(x)`` beside a bare ``fetched_at`` in a
  GROUP BY — is a SQLite-only extension that raises ``GroupingError`` on
  PostgreSQL. PG only runs here when ``TEST_POSTGRES_URL`` is set, so the
  dialect is pinned by compilation rather than by hoping the matrix ran.
- ``TestProbePathIsUnaffected`` pins the issue's one hard constraint: history is
  enrichment and can never degrade the probe path — including the ORDER of the
  Redis write vs the DB write, which a future refactor could silently invert.

Module: src/backend/db/subscriptions.py
        src/backend/services/subscription_headroom_service.py
        src/backend/services/cleanup_service.py
        src/backend/routers/subscriptions.py
Issue:  Abilityai/trinity-enterprise#433
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# IMPORTANT: set REDIS_URL BEFORE any backend import (Issue #589 hard-fail).
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend  # noqa: E402,F401

SUB = "sub-headroom-1"
OTHER = "sub-headroom-2"


def _iso(dt: datetime) -> str:
    """Match utc_now_iso(): ISO-Z with microseconds."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _ops():
    from db.subscriptions import SubscriptionOperations
    return SubscriptionOperations()


def _code_of(fn) -> str:
    """Source of `fn` with docstrings stripped.

    A source assertion that greps raw `inspect.getsource` matches the PROSE
    explaining why something is absent — so a well-documented correct
    implementation fails its own guard (the exact trap in learnings 2026-08-10:
    "a mechanism guard that greps its own function's source passes on the prose
    explaining the bug"). Here it fired in the other direction. Strip first.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _snapshot(**over):
    """A full ok-status probe snapshot, shaped exactly like `_probe` returns."""
    snap = {
        "fetched_at": _iso(datetime.now(timezone.utc)),
        "status": "ok",
        "five_hour": {"utilization_pct": 42.5, "resets_at": "2026-08-20T15:00:00Z",
                      "status": "allowed"},
        "seven_day": {"utilization_pct": 11.0, "resets_at": "2026-08-26T00:00:00Z",
                      "status": "allowed"},
        "representative_claim": "five_hour",
        "overage_status": "none",
        "unified_status": "allowed",
    }
    snap.update(over)
    return snap


# ---------------------------------------------------------------------------
# The four-file trap: this is the ONLY test that exercises db/tables.py
# ---------------------------------------------------------------------------
class TestRoundTrip:
    def test_every_column_round_trips_against_a_migrated_db(self, db_backend):
        """Insert all 13 fields, read them all back.

        A Column present in db/schema.py but missing from db/tables.py passes
        schema-parity and fails here — which is the entire point.
        """
        ops = _ops()
        snap = _snapshot()
        ops.insert_headroom_history(SUB, snap)

        rows = ops.get_headroom_history(SUB, hours=24, bucket="hour")
        assert len(rows) == 1
        r = rows[0]
        assert r["fetched_at"] == snap["fetched_at"]
        assert r["status"] == "ok"
        assert r["five_hour"]["utilization_pct"] == pytest.approx(42.5)
        assert r["five_hour"]["resets_at"] == "2026-08-20T15:00:00Z"
        assert r["five_hour"]["status"] == "allowed"
        assert r["seven_day"]["utilization_pct"] == pytest.approx(11.0)
        assert r["seven_day"]["resets_at"] == "2026-08-26T00:00:00Z"
        assert r["seven_day"]["status"] == "allowed"
        assert r["representative_claim"] == "five_hour"
        assert r["overage_status"] == "none"
        assert r["unified_status"] == "allowed"
        assert r["samples"] == 1

    def test_status_only_row_persists_with_null_windows(self, db_backend):
        """A failed probe is a row, not a skip — otherwise a dead token for
        three days is byte-identical to nobody watching."""
        ops = _ops()
        ops.insert_headroom_history(
            SUB, {"fetched_at": _iso(datetime.now(timezone.utc)), "status": "error"}
        )
        rows = ops.get_headroom_history(SUB, hours=24, bucket="hour")
        assert len(rows) == 1
        assert rows[0]["status"] == "error"
        assert rows[0]["five_hour"]["utilization_pct"] is None

    def test_rows_are_scoped_to_their_subscription(self, db_backend):
        ops = _ops()
        ops.insert_headroom_history(SUB, _snapshot())
        ops.insert_headroom_history(OTHER, _snapshot())
        assert len(ops.get_headroom_history(SUB, hours=24, bucket="hour")) == 1
        assert len(ops.get_headroom_history(OTHER, hours=24, bucket="hour")) == 1


class TestBucketing:
    def test_last_sample_wins_within_a_bucket_not_max(self, db_backend):
        """`last`, never `max`. Three reasons (requirements §20.5b); this pins
        the behaviour: the newest sample in the bucket is returned even though
        an earlier one in the same bucket has a HIGHER utilization."""
        ops = _ops()
        base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        ops.insert_headroom_history(SUB, _snapshot(
            fetched_at=_iso(base + timedelta(minutes=5)),
            five_hour={"utilization_pct": 90.0, "resets_at": None, "status": "allowed"},
        ))
        ops.insert_headroom_history(SUB, _snapshot(
            fetched_at=_iso(base + timedelta(minutes=45)),
            five_hour={"utilization_pct": 10.0, "resets_at": None, "status": "allowed"},
        ))
        rows = ops.get_headroom_history(SUB, hours=24, bucket="hour")
        assert len(rows) == 1, "both samples fall in one hour bucket"
        assert rows[0]["five_hour"]["utilization_pct"] == pytest.approx(10.0), (
            "a max-based read would return 90.0 — the observer-effect bug"
        )
        assert rows[0]["samples"] == 2

    def test_rate_limited_sample_with_null_utilization_stays_visible(self, db_backend):
        """The single most important sample in the series carries a window
        status and NO figure. Under a MAX(utilization) read it would vanish and
        the chart would flatline through an outage."""
        ops = _ops()
        ops.insert_headroom_history(SUB, _snapshot(
            status="rate_limited",
            five_hour={"utilization_pct": None, "resets_at": "2026-08-20T15:00:00Z",
                       "status": "rate_limited"},
        ))
        rows = ops.get_headroom_history(SUB, hours=24, bucket="hour")
        assert len(rows) == 1
        assert rows[0]["status"] == "rate_limited"
        assert rows[0]["five_hour"]["utilization_pct"] is None
        assert rows[0]["five_hour"]["status"] == "rate_limited"

    def test_gaps_are_absent_never_zero_filled(self, db_backend):
        """Two samples three hours apart yield TWO buckets, not four. Nothing
        is synthesised for the empty hours."""
        ops = _ops()
        base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        ops.insert_headroom_history(SUB, _snapshot(fetched_at=_iso(base - timedelta(hours=3))))
        ops.insert_headroom_history(SUB, _snapshot(fetched_at=_iso(base)))
        rows = ops.get_headroom_history(SUB, hours=24, bucket="hour")
        assert len(rows) == 2
        assert all(r["five_hour"]["utilization_pct"] is not None for r in rows)

    def test_bucket_start_is_the_logical_slot_not_the_sample_time(self, db_backend):
        """The pair (bucket_start, fetched_at) is what makes a gap decidable.
        A real timestamp alone cannot distinguish sample jitter from a missing
        bucket, so bucket_start must be the slot boundary, not the sample."""
        ops = _ops()
        base = datetime.now(timezone.utc).replace(minute=37, second=12, microsecond=0)
        ops.insert_headroom_history(SUB, _snapshot(fetched_at=_iso(base)))
        r = ops.get_headroom_history(SUB, hours=24, bucket="hour")[0]
        assert r["bucket_start"].endswith(":00:00Z")
        assert r["bucket_start"][:13] == r["fetched_at"][:13]
        assert r["bucket_start"] != r["fetched_at"]

    def test_day_bucket_start_is_midnight(self, db_backend):
        ops = _ops()
        ops.insert_headroom_history(SUB, _snapshot())
        r = ops.get_headroom_history(SUB, hours=720, bucket="day")[0]
        assert r["bucket_start"].endswith("T00:00:00Z")

    def test_window_excludes_rows_outside_the_cutoff(self, db_backend):
        ops = _ops()
        old = datetime.now(timezone.utc) - timedelta(days=9)
        ops.insert_headroom_history(SUB, _snapshot(fetched_at=_iso(old)))
        ops.insert_headroom_history(SUB, _snapshot())
        assert len(ops.get_headroom_history(SUB, hours=168, bucket="hour")) == 1
        assert len(ops.get_headroom_history(SUB, hours=720, bucket="day")) == 2


class TestDialectPortability:
    """C2: the natural shape for this read is a SQLite-only extension."""

    def test_bucket_query_compiles_for_postgresql(self):
        """`MAX(x)` beside a bare `fetched_at` in a GROUP BY is a SQLite bare-
        column extension; PostgreSQL raises GroupingError. Compiling for the PG
        dialect catches that class on every run, without needing a live PG.
        """
        from sqlalchemy import and_, func, select
        from sqlalchemy.dialects import postgresql
        from db.tables import subscription_headroom_history as t

        key = func.substr(t.c.fetched_at, 1, 13)
        ranked = (
            select(
                key.label("bucket"),
                t.c.fetched_at,
                func.row_number().over(
                    partition_by=key, order_by=t.c.fetched_at.desc()
                ).label("rn"),
            )
            .where(and_(t.c.subscription_id == "x", t.c.fetched_at > "y"))
            .subquery()
        )
        stmt = select(ranked).where(ranked.c.rn == 1)
        sql = str(stmt.compile(dialect=postgresql.dialect())).lower()
        assert "row_number" in sql
        assert "group by" not in sql, (
            "the read must not reintroduce a GROUP BY with bare columns"
        )

    def test_production_query_uses_row_number_not_bare_group_by(self):
        from db import subscriptions as subs

        code = _code_of(subs.SubscriptionOperations.get_headroom_history)
        assert "row_number" in code
        assert "group_by" not in code, (
            "a GROUP BY here would reintroduce the PostgreSQL GroupingError"
        )

    def test_bucketer_does_not_cargo_cult_the_legacy_replace(self):
        """`schedule_executions` wraps substr in replace(col,' ','T') for
        pre-#1474 space-separated rows. `fetched_at` is only ever written by
        utc_now_iso(), so that form cannot occur and the wrapper would import a
        workaround for a defect this table cannot have."""
        from db import subscriptions as subs

        assert "replace" not in _code_of(
            subs.SubscriptionOperations.get_headroom_history
        )


class TestFacadeDelegation:
    """DatabaseManager has NO __getattr__ — every accessor is hand-written."""

    @pytest.mark.parametrize("name", [
        "insert_headroom_history",
        "get_headroom_history",
        "count_headroom_history_candidates",
        "prune_headroom_history",
        "count_rate_limit_event_candidates",
        "cleanup_old_rate_limit_events",
    ])
    def test_accessor_is_delegated(self, name):
        from database import db
        assert callable(getattr(db, name, None)), (
            f"missing DatabaseManager delegation: {name}"
        )


class TestProbePathIsUnaffected:
    """The issue's one hard constraint."""

    def _svc(self):
        import services.subscription_headroom_service as svc
        return svc

    def test_history_write_failure_leaves_the_probe_result_intact(self):
        svc = self._svc()
        snap = _snapshot()
        # `_probe` is an async def, so patch.object auto-creates an AsyncMock —
        # `return_value` is what the await RESOLVES to, never a coroutine to
        # wrap by hand (doing so double-wraps and the await yields a coroutine).
        with patch.object(svc, "_probe", return_value=snap), \
             patch.object(svc, "_store_snapshot") as store, \
             patch.object(svc.db, "insert_headroom_history",
                          side_effect=RuntimeError("db is locked")):
            out = asyncio.run(svc._probe_and_store(SUB))
        assert out == snap, "a failed history write must not change the result"
        store.assert_called_once()

    def test_snapshot_is_stored_before_history(self):
        """Order is load-bearing: the Redis snapshot is what every live surface
        reads, so a slow or contended DB write must never delay it."""
        svc = self._svc()
        calls = []
        with patch.object(svc, "_probe", return_value=_snapshot()), \
             patch.object(svc, "_store_snapshot",
                          side_effect=lambda *a, **k: calls.append("redis")), \
             patch.object(svc.db, "insert_headroom_history",
                          side_effect=lambda *a, **k: calls.append("db")):
            asyncio.run(svc._probe_and_store(SUB))
        assert calls == ["redis", "db"]

    def test_no_row_when_the_probe_never_ran(self):
        """No usable token => `_probe` returns None before any HTTP call.
        Persisting that would emit a row every 15 minutes forever for a pure
        configuration state."""
        svc = self._svc()
        with patch.object(svc, "_probe", return_value=None), \
             patch.object(svc, "_store_snapshot") as store, \
             patch.object(svc.db, "insert_headroom_history") as ins:
            out = asyncio.run(svc._probe_and_store(SUB))
        assert out is None
        ins.assert_not_called()
        store.assert_not_called()

    def test_history_write_runs_off_the_event_loop(self):
        """A synchronous SQLAlchemy write on the loop stalls /health, the WS
        dispatcher and every in-flight request for up to the 30s busy timeout
        when it lands during the 03:30 backup or 04:30 VACUUM. try/except
        handles errors, not blocking — only to_thread handles both."""
        svc = self._svc()
        code = _code_of(svc._record_history)
        assert "asyncio.to_thread" in code
        assert "except Exception" in code
        assert "BaseException" not in code, (
            "CancelledError must keep propagating on shutdown"
        )


class TestStatusClassification:
    def test_ok_with_no_windows_is_recorded_as_no_windows(self):
        """`parse_unified_headers` returns non-None when only the bare top-level
        status header is present, so status='ok' with both windows None is
        reachable. Persisted verbatim it is an all-NULL row indistinguishable
        from a botched write — permanently, once it is in the table."""
        svc = self._svc()
        row = svc._history_row({"fetched_at": "x", "status": "ok"})
        assert row["status"] == "no_windows"

    def test_a_real_ok_row_is_left_alone(self):
        svc = self._svc()
        row = svc._history_row(_snapshot())
        assert row["status"] == "ok"

    def test_failure_statuses_are_left_alone(self):
        svc = self._svc()
        for s in ("error", "invalid_token", "rate_limited"):
            assert svc._history_row({"fetched_at": "x", "status": s})["status"] == s

    def test_classification_does_not_mutate_the_live_snapshot(self):
        """History-local: #471's snapshot keeps its own status vocabulary."""
        svc = self._svc()
        snap = {"fetched_at": "x", "status": "ok"}
        svc._history_row(snap)
        assert snap["status"] == "ok"

    def _svc(self):
        import services.subscription_headroom_service as svc
        return svc


class TestRetention:
    def test_prune_respects_the_window(self, db_backend):
        ops = _ops()
        ops.insert_headroom_history(SUB, _snapshot(
            fetched_at=_iso(datetime.now(timezone.utc) - timedelta(days=45))))
        ops.insert_headroom_history(SUB, _snapshot())
        assert ops.prune_headroom_history(retention_days=30) == 1
        assert len(ops.get_headroom_history(SUB, hours=720, bucket="day")) == 1

    def test_zero_disables_the_sweep(self, db_backend):
        ops = _ops()
        ops.insert_headroom_history(SUB, _snapshot(
            fetched_at=_iso(datetime.now(timezone.utc) - timedelta(days=999))))
        assert ops.prune_headroom_history(retention_days=0) == 0

    def test_count_and_prune_share_one_predicate(self):
        """A guard that counts a different row set than the prune removes is a
        guard over nothing."""
        import inspect
        from db import subscriptions as subs

        for fn in (subs.SubscriptionOperations.count_headroom_history_candidates,
                   subs.SubscriptionOperations.prune_headroom_history):
            assert "_headroom_history_prune_predicate" in inspect.getsource(fn)
        for fn in (subs.SubscriptionOperations.count_rate_limit_event_candidates,
                   subs.SubscriptionOperations.cleanup_old_rate_limit_events):
            assert "_rate_limit_event_prune_predicate" in inspect.getsource(fn)

    def test_candidate_count_is_bounded_by_limit(self, db_backend):
        ops = _ops()
        old = datetime.now(timezone.utc) - timedelta(days=45)
        for i in range(5):
            ops.insert_headroom_history(SUB, _snapshot(
                fetched_at=_iso(old + timedelta(seconds=i))))
        assert ops.count_headroom_history_candidates(30, 3) == 3
        assert ops.count_headroom_history_candidates(30, 100) == 5

    def test_both_keys_are_registered_retention_windows(self):
        from services.settings_service import (
            NON_ROW_RETENTION_OPS_KEYS,
            OPS_SETTINGS_DEFAULTS,
            RETENTION_OPS_KEYS,
        )
        for key in ("subscription_headroom_retention_days",
                    "subscription_failure_event_retention_days"):
            assert key in RETENTION_OPS_KEYS
            assert key in OPS_SETTINGS_DEFAULTS
            assert int(OPS_SETTINGS_DEFAULTS[key]) == 30
            assert key not in NON_ROW_RETENTION_OPS_KEYS, "both are ROW sweeps"

    def test_neither_key_is_in_the_community_seed(self):
        """The 5-day floor would silently truncate a 7-day default read window
        while the UI labelled it 7 days."""
        from config import COMMUNITY_FRESH_INSTALL_SEED
        assert "subscription_headroom_retention_days" not in COMMUNITY_FRESH_INSTALL_SEED
        assert "subscription_failure_event_retention_days" not in COMMUNITY_FRESH_INSTALL_SEED

    def test_both_keys_are_range_validated(self):
        from config import validate_ops_setting
        for key in ("subscription_headroom_retention_days",
                    "subscription_failure_event_retention_days"):
            assert validate_ops_setting(key, "30") == "30"
            with pytest.raises(ValueError):
                validate_ops_setting(key, "not-a-number")
            with pytest.raises(ValueError):
                validate_ops_setting(key, "-1")


class TestFailureEventRetention:
    """ent#433 converts a hardcoded 24h sweep into a real window."""

    def test_prune_respects_the_configured_window(self, db_backend):
        ops = _ops()
        ops.record_rate_limit_event("a1", SUB, "429", failure_kind="rate_limit")
        assert ops.cleanup_old_rate_limit_events(retention_days=30) == 0, (
            "a fresh event is well inside a 30-day window — the old hardcoded "
            "24h sweep is what this replaces"
        )

    def test_zero_disables_the_sweep(self, db_backend):
        ops = _ops()
        ops.record_rate_limit_event("a1", SUB, "429", failure_kind="rate_limit")
        assert ops.cleanup_old_rate_limit_events(retention_days=0) == 0

    def test_default_widened_and_never_narrowed(self):
        """Widening is the #1638-safe direction. Pin the DIRECTION so a future
        edit cannot quietly restore a destructive window."""
        from services.settings_service import OPS_SETTINGS_DEFAULTS
        assert int(OPS_SETTINGS_DEFAULTS["subscription_failure_event_retention_days"]) >= 30


class TestCascade:
    def test_deleting_a_subscription_removes_its_history(self, db_backend):
        from sqlalchemy import insert
        from db.engine import get_engine
        from db.tables import subscription_credentials

        with get_engine().begin() as conn:
            conn.execute(insert(subscription_credentials).values(
                id=SUB, name="s1", encrypted_credentials="x", owner_id=1,
                created_at=_iso(datetime.now(timezone.utc)),
                updated_at=_iso(datetime.now(timezone.utc)),
            ))
        ops = _ops()
        ops.insert_headroom_history(SUB, _snapshot())
        ops.insert_headroom_history(OTHER, _snapshot())

        assert ops.delete_subscription(SUB) is True
        assert ops.get_headroom_history(SUB, hours=24, bucket="hour") == []
        assert len(ops.get_headroom_history(OTHER, hours=24, bucket="hour")) == 1, (
            "the cascade must be scoped to the deleted subscription"
        )


class TestServiceReadSurface:
    def test_windows_map_to_the_documented_granularity(self):
        from services.subscription_headroom_service import HISTORY_WINDOWS
        assert HISTORY_WINDOWS["24h"]["bucket"] == "hour"
        assert HISTORY_WINDOWS["7d"]["bucket"] == "hour"
        assert HISTORY_WINDOWS["30d"]["bucket"] == "day"
        assert HISTORY_WINDOWS["7d"]["hours"] == 168
        assert HISTORY_WINDOWS["30d"]["hours"] == 720

    def test_coverage_reports_a_thin_series_as_thin(self, db_backend):
        """So a sparse chart states its own sparseness instead of rendering as
        a confident flat line."""
        from services.subscription_headroom_service import get_history
        ops = _ops()
        base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        for h in range(3):
            ops.insert_headroom_history(SUB, _snapshot(
                fetched_at=_iso(base - timedelta(hours=h))))
        out = get_history(SUB, "24h")
        assert len(out["buckets"]) == 3
        assert out["coverage_pct"] == pytest.approx(12.5)  # 3/24
        assert out["bucket"] == "hour"
        assert out["window"] == "24h"

    def test_empty_series_is_zero_coverage_not_an_error(self, db_backend):
        from services.subscription_headroom_service import get_history
        out = get_history("nobody", "7d")
        assert out["buckets"] == []
        assert out["coverage_pct"] == 0.0

    def test_read_never_probes(self):
        """Viewing a trend must cost no subscription quota — history records
        what already happened, and a sparse chart must never become a reason to
        probe more often."""
        import inspect
        from services import subscription_headroom_service as svc

        src = inspect.getsource(svc.get_history)
        assert "_probe" not in src and "get_headroom(" not in src


