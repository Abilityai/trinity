"""
Tests for the Redis-side orphan-slot sweep on backend startup (#749).

Companion to #748: the SQL-side recovery walks `running` rows in SQL and
flips them to FAILED when Redis lost their slot. #749 closes the inverse
asymmetry — when the backend is killed between `capacity.acquire()` (ZADD
on `agent:slots:{name}`) and the `finally`-block `capacity.release()`
(ZREM), the slot is leaked because no SQL row marks "this execution is
still active." Startup recovery must SCAN Redis and ZREM members whose
SQL row is terminal or missing.

Covered:

  - SCAN walks every agent:slots:* key.
  - Drain sentinel members (prefix `drain-`) are skipped.
  - Members whose ZSET score is within the grace window are skipped
    (mirrors the SQL-side `STARTUP_RECOVERY_GRACE_SECONDS` pattern).
  - Members with terminal SQL status → ZREM + delete metadata.
  - Members with missing SQL row → ZREM + delete metadata.
  - Members with `running` SQL row → left alone.
  - The two-pass `recover_orphaned_executions` correctly composes both
    sides and reports `redis_slots_reclaimed`.

Mocks Redis end-to-end via a `FakeRedis` so tests don't depend on a live
broker and can deterministically pin the SCAN/ZRANGE/ZREM contract.
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Backend on sys.path; stub docker (services/__init__.py imports it eagerly)
# ---------------------------------------------------------------------------

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


@pytest.fixture(autouse=True)
def _stub_docker_and_database(monkeypatch):
    """The unit test venv does not have the `docker` package; stub it +
    services.docker_service before cleanup_service is loaded. Mirrors
    tests/unit/test_startup_recovery_race.py."""
    if "docker" not in sys.modules:
        monkeypatch.setitem(sys.modules, "docker", MagicMock(name="docker_stub"))
    if "services.docker_service" not in sys.modules:
        _ds_stub = types.ModuleType("services.docker_service")
        _ds_stub.get_docker_client = MagicMock(return_value=MagicMock())
        _ds_stub.get_agent_container = lambda *a, **kw: None
        _ds_stub.get_agent_status_from_container = lambda *a, **kw: "stopped"
        _ds_stub.list_all_agents = lambda *a, **kw: []
        _ds_stub.get_agent_by_name = lambda *a, **kw: None
        _ds_stub.get_next_available_port = lambda *a, **kw: 2222

        async def _exec(*a, **kw):
            return {"exit_code": 0, "output": ""}

        _ds_stub.execute_command_in_container = _exec
        monkeypatch.setitem(sys.modules, "services.docker_service", _ds_stub)
    if "database" not in sys.modules:
        monkeypatch.setitem(sys.modules, "database", MagicMock(name="database_stub"))


# ---------------------------------------------------------------------------
# FakeRedis: only the surface area the sweep + slot_service use
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal sync Redis double for SCAN/ZRANGE/ZREM/DELETE used by the sweep."""

    def __init__(self):
        # {key: {member: score}}
        self.zsets: dict[str, dict[str, float]] = {}
        # Plain key/value (metadata keys)
        self.kv: dict[str, bytes] = {}

    # ZADD-equivalent test helper (the sweep itself never writes ZADDs).
    def _zadd(self, key: str, member: str, score: float) -> None:
        self.zsets.setdefault(key, {})[member] = score

    def zrange(self, key: str, start: int, end: int, withscores: bool = False):
        members = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        if end == -1:
            sliced = members[start:]
        else:
            sliced = members[start:end + 1]
        if withscores:
            return [(m, s) for m, s in sliced]
        return [m for m, _ in sliced]

    def scan(self, cursor: int, match: str = "*", count: int = 200):
        # Single-shot; ignore cursor pagination — fine for fixture-scale tests.
        prefix = match.rstrip("*")
        keys = [k for k in self.zsets.keys() if k.startswith(prefix)]
        return 0, keys

    def zrem(self, key: str, member: str) -> int:
        if key in self.zsets and member in self.zsets[key]:
            del self.zsets[key][member]
            return 1
        return 0

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def delete(self, key: str) -> int:
        gone = 0
        if key in self.zsets:
            del self.zsets[key]
            gone += 1
        if key in self.kv:
            del self.kv[key]
            gone += 1
        return gone


def _make_status_row(status: str):
    """Build a minimal stand-in for ScheduleExecution with a `.status` attr."""
    row = MagicMock()
    row.status = status
    return row


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Direct sweep behaviour
# ---------------------------------------------------------------------------


class TestReconcileOrphanedSlots:
    """`_reconcile_orphaned_slots` — Redis→SQL direction."""

    def _patched_sweep(self, fake_redis, sql_lookup):
        """Returns the patched sweep function with a stub slot_service that
        points at `fake_redis`. `sql_lookup` is a callable
        (execution_id) -> status_row | None."""
        from services import cleanup_service as cs

        fake_slot_service = MagicMock()
        fake_slot_service.redis = fake_redis
        fake_slot_service.slots_prefix = "agent:slots:"
        fake_slot_service._metadata_key = (
            lambda agent, eid: f"agent:slot-meta:{agent}:{eid}"
        )

        ctx = patch.object(cs, "db")
        mock_db = ctx.start()
        mock_db.get_execution.side_effect = sql_lookup

        ctx2 = patch.dict(
            sys.modules,
            {"services.slot_service": types.SimpleNamespace(
                get_slot_service=lambda: fake_slot_service
            )},
        )
        ctx2.start()
        return cs._reconcile_orphaned_slots, (ctx, ctx2)

    def _teardown(self, ctxs):
        for c in ctxs:
            c.stop()

    def test_orphan_with_missing_sql_row_is_zrem(self):
        fake = _FakeRedis()
        # Slot present, score well below grace cutoff (old).
        fake._zadd("agent:slots:agent-a", "exec-orphan", time.time() - 600)

        sweep, ctxs = self._patched_sweep(fake, sql_lookup=lambda _: None)
        try:
            result = _run(sweep())
        finally:
            self._teardown(ctxs)

        assert result == {"agent-a": 1}
        assert "exec-orphan" not in fake.zsets["agent:slots:agent-a"]

    def test_orphan_with_terminal_sql_row_is_zrem(self):
        fake = _FakeRedis()
        fake._zadd("agent:slots:agent-a", "exec-done", time.time() - 600)

        rows = {"exec-done": _make_status_row("success")}
        sweep, ctxs = self._patched_sweep(fake, sql_lookup=lambda eid: rows.get(eid))
        try:
            result = _run(sweep())
        finally:
            self._teardown(ctxs)

        assert result == {"agent-a": 1}
        assert "exec-done" not in fake.zsets["agent:slots:agent-a"]

    def test_running_sql_row_is_left_alone(self):
        fake = _FakeRedis()
        fake._zadd("agent:slots:agent-a", "exec-live", time.time() - 600)

        rows = {"exec-live": _make_status_row("running")}
        sweep, ctxs = self._patched_sweep(fake, sql_lookup=lambda eid: rows.get(eid))
        try:
            result = _run(sweep())
        finally:
            self._teardown(ctxs)

        assert result == {}
        assert "exec-live" in fake.zsets["agent:slots:agent-a"]

    def test_within_grace_window_is_skipped_even_if_orphan(self):
        """A brand-new ZADD whose SQL row hasn't been written yet must NOT
        be swept — that's the symmetric companion to the #748 SQL grace
        window. The race is the inverse: handler did ZADD, didn't yet
        insert SQL row; sweep would falsely reclaim a live slot.
        """
        from services.cleanup_service import SLOT_RECOVERY_GRACE_SECONDS

        fake = _FakeRedis()
        # Just-ZADDed (now), within grace.
        fake._zadd("agent:slots:agent-a", "exec-justborn", time.time() - 1)
        # Also seed an old orphan so we know the sweep ran and only skipped
        # the fresh one.
        fake._zadd("agent:slots:agent-a", "exec-orphan", time.time() - 600)

        sweep, ctxs = self._patched_sweep(fake, sql_lookup=lambda _: None)
        try:
            result = _run(sweep())
        finally:
            self._teardown(ctxs)

        assert result == {"agent-a": 1}
        assert "exec-justborn" in fake.zsets["agent:slots:agent-a"]
        assert "exec-orphan" not in fake.zsets["agent:slots:agent-a"]

        # Sanity: grace window matches the documented constant.
        assert SLOT_RECOVERY_GRACE_SECONDS == 15

    def test_drain_sentinel_is_skipped(self):
        """Members starting with 'drain-' are capacity-manager sentinels,
        not real executions. Mirrors the canary S-01 filter."""
        fake = _FakeRedis()
        fake._zadd("agent:slots:agent-a", "drain-agent-a-001", time.time() - 600)

        sweep, ctxs = self._patched_sweep(fake, sql_lookup=lambda _: None)
        try:
            result = _run(sweep())
        finally:
            self._teardown(ctxs)

        assert result == {}
        # Sentinel still in place — sweep did not touch it.
        assert "drain-agent-a-001" in fake.zsets["agent:slots:agent-a"]

    def test_metadata_key_is_deleted_with_member(self):
        """ZREM is paired with DELETE of the per-execution metadata key
        (matches slot_service._cleanup_stale_slots_for_agent)."""
        fake = _FakeRedis()
        fake._zadd("agent:slots:agent-a", "exec-orphan", time.time() - 600)
        meta_key = "agent:slot-meta:agent-a:exec-orphan"
        fake.kv[meta_key] = b'{"some": "meta"}'

        sweep, ctxs = self._patched_sweep(fake, sql_lookup=lambda _: None)
        try:
            _run(sweep())
        finally:
            self._teardown(ctxs)

        assert meta_key not in fake.kv

    def test_multiple_agents_aggregated(self):
        fake = _FakeRedis()
        fake._zadd("agent:slots:agent-a", "exec-a1", time.time() - 600)
        fake._zadd("agent:slots:agent-a", "exec-a2", time.time() - 700)
        fake._zadd("agent:slots:agent-b", "exec-b1", time.time() - 500)

        sweep, ctxs = self._patched_sweep(fake, sql_lookup=lambda _: None)
        try:
            result = _run(sweep())
        finally:
            self._teardown(ctxs)

        assert result == {"agent-a": 2, "agent-b": 1}
        # Every member gone.
        assert fake.zsets["agent:slots:agent-a"] == {}
        assert fake.zsets["agent:slots:agent-b"] == {}

    def test_redis_failure_returns_empty_does_not_raise(self):
        """If get_slot_service() raises (Redis unreachable at startup),
        the sweep must not crash recovery — it returns {} so the outer
        function still completes successfully."""
        from services import cleanup_service as cs

        with patch.dict(
            sys.modules,
            {"services.slot_service": types.SimpleNamespace(
                get_slot_service=MagicMock(side_effect=ConnectionError("redis down")),
            )},
        ):
            result = _run(cs._reconcile_orphaned_slots())

        assert result == {}


# ---------------------------------------------------------------------------
# Integration: recover_orphaned_executions composes both passes
# ---------------------------------------------------------------------------


class TestRecoverOrphanedExecutionsIncludesRedisSweep:
    """The startup hook must call both passes and report both counts."""

    def test_empty_sql_still_sweeps_redis(self):
        """The textbook bug shape: zero SQL running rows + a Redis slot
        that nobody owns. Without the new Redis pass we'd silently return
        {recovered: 0} and the leak persists until TTL."""
        from services import cleanup_service as cs

        fake_redis = _FakeRedis()
        fake_redis._zadd("agent:slots:agent-a", "exec-orphan", time.time() - 600)

        fake_slot_service = MagicMock()
        fake_slot_service.redis = fake_redis
        fake_slot_service.slots_prefix = "agent:slots:"
        fake_slot_service._metadata_key = (
            lambda agent, eid: f"agent:slot-meta:{agent}:{eid}"
        )

        with patch.object(cs, "db") as mock_db, patch.dict(
            sys.modules,
            {"services.slot_service": types.SimpleNamespace(
                get_slot_service=lambda: fake_slot_service
            )},
        ):
            mock_db.get_running_executions.return_value = []
            mock_db.get_execution.return_value = None

            result = _run(cs.recover_orphaned_executions())

        assert result["recovered"] == 0
        assert result["redis_slots_reclaimed"] == 1
        assert "exec-orphan" not in fake_redis.zsets["agent:slots:agent-a"]


# ---------------------------------------------------------------------------
# Source-level pin: constants documented + wired together
# ---------------------------------------------------------------------------


def test_constants_are_documented():
    """If someone deletes the constants, this catches it."""
    from services import cleanup_service as cs

    assert hasattr(cs, "SLOT_RECOVERY_GRACE_SECONDS")
    assert cs.SLOT_RECOVERY_GRACE_SECONDS > 0
    assert "success" in cs._TERMINAL_EXECUTION_STATUSES
    assert "failed" in cs._TERMINAL_EXECUTION_STATUSES
    assert "cancelled" in cs._TERMINAL_EXECUTION_STATUSES
    assert "skipped" in cs._TERMINAL_EXECUTION_STATUSES
    assert "running" not in cs._TERMINAL_EXECUTION_STATUSES
    # Drain prefix matches the canary filter constant.
    assert cs._DRAIN_SENTINEL_PREFIX == "drain-"
