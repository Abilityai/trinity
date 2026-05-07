"""
Canary invariant harness unit tests (CANARY-001 / Issue #411 — Phase 1).

Covers:
- CanaryOperations: insert (with validation), list/count with filters,
  latest-per-invariant, stats aggregation
- Snapshot collector: agent_ownership read, per-agent execution
  partitioning, orphan-ref scan, terminal-execution window
- Invariant library: S-01 (slot–row bijection), E-02 (phantom reversal
  detection via state comparison), L-03 (delete-cascade orphan scan)
- End-to-end Option-1 smoke fixture: orphan agent_sharing row triggers
  exactly one L-03 violation with correct severity

Tests run with isolated temp SQLite + an in-memory fake Redis. No live
backend required.
"""

import json
import os
import sqlite3
import sys
import tempfile
import types
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List

import pytest

# Add backend to path for direct imports.
_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "backend")
)
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)

# Stub utils.helpers (the test harness shadows src/backend/utils otherwise).
from datetime import timedelta as _td

if "utils.helpers" not in sys.modules:
    _helpers = types.ModuleType("utils.helpers")
    _helpers.utc_now = lambda: datetime.utcnow()
    _helpers.utc_now_iso = lambda: datetime.utcnow().isoformat() + "Z"
    _helpers.to_utc_iso = lambda v: str(v)
    _helpers.parse_iso_timestamp = lambda s: datetime.fromisoformat(s.rstrip("Z"))
    _helpers.iso_cutoff = lambda hours=0, minutes=0, seconds=0: (
        (datetime.utcnow() - _td(hours=hours, minutes=minutes, seconds=seconds))
        .isoformat() + "Z"
    )
    sys.modules["utils.helpers"] = _helpers


# Stub `croniter` so importing `db.__init__` doesn't fail outside the
# backend container. The canary code path never calls into croniter.
if "croniter" not in sys.modules:
    _croniter_mod = types.ModuleType("croniter")
    _croniter_mod.croniter = type("croniter", (), {})
    sys.modules["croniter"] = _croniter_mod


# ---------------------------------------------------------------------------
# Tiny in-memory Redis substitute — covers only the surface canary uses.
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal Redis stand-in for canary tests (ZSET + HASH + SCAN)."""

    def __init__(self):
        self._zsets: Dict[str, Dict[str, float]] = defaultdict(dict)
        self._hashes: Dict[str, Dict[str, str]] = defaultdict(dict)
        self._strings: Dict[str, str] = {}

    # ZSET ------------------------------------------------------------------

    def zadd(self, key: str, mapping: Dict[str, float]) -> int:
        added = 0
        for member, score in mapping.items():
            if member not in self._zsets[key]:
                added += 1
            self._zsets[key][member] = score
        return added

    def zrange(self, key: str, start: int, end: int) -> List[str]:
        items = sorted(self._zsets.get(key, {}).items(), key=lambda kv: kv[1])
        if end == -1:
            sliced = items[start:]
        else:
            sliced = items[start : end + 1]
        return [m for m, _ in sliced]

    def zcard(self, key: str) -> int:
        return len(self._zsets.get(key, {}))

    def zrem(self, key: str, member: str) -> int:
        if member in self._zsets.get(key, {}):
            del self._zsets[key][member]
            return 1
        return 0

    # HASH ------------------------------------------------------------------

    def hset(self, key: str, field: str = None, value: str = None, mapping: Dict[str, str] = None) -> int:
        added = 0
        if mapping:
            for k, v in mapping.items():
                if k not in self._hashes[key]:
                    added += 1
                self._hashes[key][k] = v
        elif field is not None:
            if field not in self._hashes[key]:
                added = 1
            self._hashes[key][field] = value
        return added

    def hget(self, key: str, field: str):
        return self._hashes.get(key, {}).get(field)

    def hkeys(self, key: str) -> List[str]:
        return list(self._hashes.get(key, {}).keys())

    def hlen(self, key: str) -> int:
        return len(self._hashes.get(key, {}))

    def delete(self, key: str) -> int:
        deleted = 0
        if key in self._zsets:
            del self._zsets[key]
            deleted += 1
        if key in self._hashes:
            del self._hashes[key]
            deleted += 1
        return deleted

    # SCAN ------------------------------------------------------------------

    def scan(self, cursor: int = 0, match: str = "*", count: int = 100):
        # No real cursoring — return everything once, then stop.
        if cursor != 0:
            return 0, []
        import fnmatch

        keys = list(self._zsets.keys()) + list(self._hashes.keys())
        matched = [k for k in keys if fnmatch.fnmatch(k, match)]
        return 0, matched

    # STRING ----------------------------------------------------------------
    # Used by CanaryService for the previous-cycle snapshot_time cursor
    # (REDIS_KEY_LAST_CYCLE) — see services/canary_service.py.

    def get(self, key: str):
        return self._strings.get(key)

    def set(self, key: str, value: str) -> bool:
        self._strings[key] = str(value)
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def canary_db(monkeypatch):
    """Temp SQLite with the tables canary touches; patch db.connection."""
    db_file = tempfile.NamedTemporaryFile(suffix="_canary_test.db", delete=False)
    db_file.close()
    db_path = db_file.name

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE canary_violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invariant_id TEXT NOT NULL,
            tier TEXT NOT NULL,
            severity TEXT NOT NULL,
            snapshot_time TEXT NOT NULL,
            observed_state TEXT NOT NULL,
            signal_query TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE agent_ownership (
            agent_name TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            is_system INTEGER DEFAULT 0,
            max_parallel_tasks INTEGER DEFAULT 3,
            execution_timeout_seconds INTEGER DEFAULT 900
        );
        CREATE TABLE schedule_executions (
            id TEXT PRIMARY KEY,
            schedule_id TEXT,
            agent_name TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            message TEXT NOT NULL DEFAULT '',
            triggered_by TEXT NOT NULL DEFAULT 'test'
        );
        CREATE TABLE agent_sharing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            shared_with_email TEXT NOT NULL,
            shared_by_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE agent_schedules (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            cron_expression TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            enabled INTEGER DEFAULT 1,
            owner_id INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE chat_sessions (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            user_id INTEGER NOT NULL DEFAULT 0,
            user_email TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            last_message_at TEXT NOT NULL DEFAULT '',
            status TEXT DEFAULT 'active'
        );
        CREATE TABLE agent_skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            skill_name TEXT NOT NULL
        );
        CREATE TABLE agent_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            tag TEXT NOT NULL
        );
        CREATE TABLE agent_shared_files (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            download_token TEXT UNIQUE NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE agent_public_links (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            token TEXT NOT NULL
        );
        CREATE TABLE operator_queue (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            priority TEXT NOT NULL DEFAULT 'medium',
            title TEXT NOT NULL DEFAULT '',
            question TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE access_requests (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            email TEXT NOT NULL,
            requested_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE TABLE mcp_api_keys (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            agent_name TEXT,
            scope TEXT NOT NULL,
            key_hash TEXT UNIQUE NOT NULL,
            key_prefix TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.commit()
    conn.close()

    class _ConnCtx:
        def __enter__(self):
            self._conn = sqlite3.connect(db_path)
            self._conn.row_factory = sqlite3.Row
            return self._conn

        def __exit__(self, exc_type, exc_val, exc_tb):
            try:
                if exc_type is None:
                    self._conn.commit()
                else:
                    self._conn.rollback()
            finally:
                self._conn.close()

    fake_db_connection = types.ModuleType("db.connection")
    fake_db_connection.get_db_connection = lambda: _ConnCtx()
    monkeypatch.setitem(sys.modules, "db.connection", fake_db_connection)

    yield db_path
    os.unlink(db_path)


@pytest.fixture
def fake_redis(monkeypatch):
    """Patch services.slot_service.get_slot_service to a fake."""
    redis_inst = FakeRedis()

    class _FakeSlotService:
        slots_prefix = "agent:slots:"

        def __init__(self):
            self.redis = redis_inst

    fake_module = types.ModuleType("services.slot_service")
    fake_module.get_slot_service = lambda: _FakeSlotService()
    monkeypatch.setitem(sys.modules, "services.slot_service", fake_module)

    return redis_inst


@pytest.fixture
def reload_canary(canary_db, fake_redis):
    """Force reimport of canary modules so they bind to the patched modules."""
    for mod in list(sys.modules):
        if mod.startswith("canary") or mod == "db.canary":
            del sys.modules[mod]
    import canary as canary_pkg  # noqa: F401
    import db.canary as db_canary

    return {"canary": canary_pkg, "db_canary": db_canary, "redis": fake_redis}


# Override the package-wide autouse fixtures.
@pytest.fixture(scope="session")
def api_client():
    yield None


@pytest.fixture(autouse=True)
def cleanup_after_test():
    yield


# ---------------------------------------------------------------------------
# Helpers — populate fixtures
# ---------------------------------------------------------------------------


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _add_agent(path, name, max_parallel=3, timeout=900, is_system=0):
    c = _conn(path)
    c.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, is_system, max_parallel_tasks, execution_timeout_seconds) VALUES (?, ?, ?, ?, ?)",
        (name, "test-owner", is_system, max_parallel, timeout),
    )
    c.commit()
    c.close()


def _add_execution(path, eid, agent_name, status, started_at=None, completed_at=None):
    c = _conn(path)
    c.execute(
        "INSERT INTO schedule_executions (id, agent_name, status, started_at, completed_at) VALUES (?, ?, ?, ?, ?)",
        (eid, agent_name, status, started_at or "2026-04-30T00:00:00Z", completed_at),
    )
    c.commit()
    c.close()


def _add_orphan_sharing(path, agent_name):
    c = _conn(path)
    c.execute(
        "INSERT INTO agent_sharing (agent_name, shared_with_email, shared_by_id) VALUES (?, ?, ?)",
        (agent_name, "ghost@example.com", "test-owner"),
    )
    c.commit()
    c.close()


# ---------------------------------------------------------------------------
# CanaryOperations tests
# ---------------------------------------------------------------------------


class TestCanaryOperations:
    def test_insert_and_fetch(self, reload_canary):
        ops = reload_canary["db_canary"].CanaryOperations()
        rid = ops.insert_violation(
            invariant_id="S-01",
            tier="A",
            severity="critical",
            snapshot_time="2026-04-30T12:00:00Z",
            observed_state={"agent": "a", "redis": 1},
        )
        assert rid > 0
        v = ops.get_violation(rid)
        assert v["invariant_id"] == "S-01"
        # observed_state is parsed back to dict
        assert v["observed_state"]["agent"] == "a"

    def test_insert_validates_tier(self, reload_canary):
        ops = reload_canary["db_canary"].CanaryOperations()
        with pytest.raises(ValueError, match="invalid tier"):
            ops.insert_violation("S-01", "X", "critical", "t", {})

    def test_insert_validates_severity(self, reload_canary):
        ops = reload_canary["db_canary"].CanaryOperations()
        with pytest.raises(ValueError, match="invalid severity"):
            ops.insert_violation("S-01", "A", "fatal", "t", {})

    def test_filters_and_count(self, reload_canary):
        ops = reload_canary["db_canary"].CanaryOperations()
        ops.insert_violation("S-01", "A", "critical", "2026-04-30T12:00:00Z", {})
        ops.insert_violation("S-01", "A", "major", "2026-04-30T12:05:00Z", {})
        ops.insert_violation("E-02", "A", "critical", "2026-04-30T12:05:00Z", {})

        assert ops.count_violations() == 3
        assert ops.count_violations(invariant_id="S-01") == 2
        assert ops.count_violations(severity="critical") == 2
        assert (
            ops.count_violations(start_time="2026-04-30T12:03:00Z") == 2
        ), "time-window filter must use lexicographic ISO-Z compare"

    def test_latest_per_invariant(self, reload_canary):
        ops = reload_canary["db_canary"].CanaryOperations()
        ops.insert_violation("S-01", "A", "critical", "2026-04-30T12:00:00Z", {})
        latest_s01 = ops.insert_violation(
            "S-01", "A", "critical", "2026-04-30T12:05:00Z", {}
        )
        latest_e02 = ops.insert_violation(
            "E-02", "A", "critical", "2026-04-30T12:05:00Z", {}
        )

        latest = ops.get_latest_per_invariant()
        assert latest["S-01"]["id"] == latest_s01
        assert latest["E-02"]["id"] == latest_e02

    def test_stats(self, reload_canary):
        ops = reload_canary["db_canary"].CanaryOperations()
        ops.insert_violation("S-01", "A", "critical", "2026-04-30T12:00:00Z", {})
        ops.insert_violation("S-01", "A", "major", "2026-04-30T12:05:00Z", {})
        ops.insert_violation("L-03", "A", "critical", "2026-04-30T12:10:00Z", {})

        stats = ops.stats_by_invariant()
        assert stats["total"] == 3
        assert stats["by_invariant"] == {"S-01": 2, "L-03": 1}
        assert stats["by_severity"] == {"critical": 2, "major": 1}


# ---------------------------------------------------------------------------
# Snapshot collector tests
# ---------------------------------------------------------------------------


class TestSnapshotCollector:
    def test_empty_platform(self, reload_canary):
        snap = reload_canary["canary"].collect_snapshot()
        assert snap.known_agents == set()
        assert snap.agents == []
        assert snap.orphan_refs == []
        assert snap.sources_unavailable == []

    def test_agents_partitioned_by_status(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        _add_execution(canary_db, "e-run-1", "a1", "running")
        _add_execution(canary_db, "e-q-1", "a1", "queued")
        _add_execution(canary_db, "e-done", "a1", "success")

        snap = reload_canary["canary"].collect_snapshot()
        assert snap.known_agents == {"a1"}
        assert len(snap.agents) == 1
        agent = snap.agents[0]
        assert agent.running_exec_ids == {"e-run-1"}
        assert agent.queued_exec_ids == {"e-q-1"}

    def test_redis_slots_collected(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        redis = reload_canary["redis"]
        redis.zadd("agent:slots:a1", {"e-run-1": 1.0, "drain-a1-9": 2.0})

        snap = reload_canary["canary"].collect_snapshot()
        agent = snap.agents[0]
        assert agent.slot_ids == {"e-run-1", "drain-a1-9"}

    def test_orphan_redis_slots_for_unknown_agent(self, canary_db, reload_canary):
        _add_agent(canary_db, "real")
        redis = reload_canary["redis"]
        redis.zadd("agent:slots:ghost", {"e-1": 1.0})

        snap = reload_canary["canary"].collect_snapshot()
        assert snap.orphan_redis_slots == {"ghost": 1}

    def test_orphan_ref_scan(self, canary_db, reload_canary):
        _add_agent(canary_db, "real")
        _add_orphan_sharing(canary_db, "ghost-1")
        _add_orphan_sharing(canary_db, "ghost-2")

        snap = reload_canary["canary"].collect_snapshot()
        ghost_names = {r.referenced_agent_name for r in snap.orphan_refs}
        assert ghost_names == {"ghost-1", "ghost-2"}

    def test_terminal_executions_window(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        # Recent terminal — included
        _add_execution(
            canary_db, "e-recent", "a1", "success",
            completed_at=datetime.utcnow().isoformat(),
        )
        # Old terminal — excluded by 30-min window
        _add_execution(
            canary_db, "e-old", "a1", "success",
            completed_at="2025-01-01T00:00:00",
        )
        snap = reload_canary["canary"].collect_snapshot()
        assert "e-recent" in snap.terminal_exec_ids
        assert "e-old" not in snap.terminal_exec_ids


# ---------------------------------------------------------------------------
# Invariant: S-01 slot–row bijection
# ---------------------------------------------------------------------------


class TestInvariantS01:
    def test_holds_when_sets_match(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        _add_execution(canary_db, "e1", "a1", "running")
        reload_canary["redis"].zadd("agent:slots:a1", {"e1": 1.0})

        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s01_slot_row_bijection as s01

        assert s01.check(snap) == []

    def test_fires_when_redis_has_phantom(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        _add_execution(canary_db, "e1", "a1", "running")
        # Phantom in Redis only.
        reload_canary["redis"].zadd("agent:slots:a1", {"e1": 1.0, "phantom": 2.0})

        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s01_slot_row_bijection as s01

        violations = s01.check(snap)
        assert len(violations) == 1
        v = violations[0]
        assert v.invariant_id == "S-01"
        assert v.severity == "critical"
        assert v.observed_state["in_redis_only"] == ["phantom"]
        assert v.observed_state["in_sql_only"] == []
        assert v.observed_state["agent_name"] == "a1"

    def test_drain_sentinels_ignored(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        _add_execution(canary_db, "e1", "a1", "running")
        reload_canary["redis"].zadd(
            "agent:slots:a1", {"e1": 1.0, "drain-a1-12345": 2.0}
        )
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s01_slot_row_bijection as s01

        assert s01.check(snap) == [], "drain sentinels must not trip S-01"

    def test_fires_when_sql_orphan(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        _add_execution(canary_db, "e-running-no-slot", "a1", "running")
        # No Redis slot.

        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s01_slot_row_bijection as s01

        violations = s01.check(snap)
        assert len(violations) == 1
        assert violations[0].observed_state["in_sql_only"] == ["e-running-no-slot"]

    def test_skipped_when_redis_unavailable(self, reload_canary):
        from canary.snapshot import Snapshot, AgentSnapshot

        snap = Snapshot(
            snapshot_time="2026-04-30T12:00:00Z",
            sources_unavailable=["redis: connection refused"],
            agents=[
                AgentSnapshot(
                    name="a1",
                    is_system=False,
                    max_parallel=3,
                    execution_timeout_seconds=900,
                    slot_ids=set(),
                    running_exec_ids={"e1"},
                )
            ],
        )
        from canary.invariants import s01_slot_row_bijection as s01

        # Even with mismatch, must not fire if Redis was unreachable.
        assert s01.check(snap) == []


# ---------------------------------------------------------------------------
# Invariant: E-02 phantom reversal
# ---------------------------------------------------------------------------


class TestInvariantE02:
    def test_holds_on_first_cycle(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        _add_execution(
            canary_db, "e-done", "a1", "success",
            completed_at=datetime.utcnow().isoformat(),
        )
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import e02_no_phantom_reversal as e02

        assert e02.check(snap) == []

    def test_fires_on_terminal_to_running_reversal(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        # Cycle 1: e-done is terminal.
        _add_execution(
            canary_db, "e-done", "a1", "success",
            completed_at=datetime.utcnow().isoformat(),
        )
        snap1 = reload_canary["canary"].collect_snapshot()
        from canary.invariants import e02_no_phantom_reversal as e02

        # First call seeds the side-table with terminal ids.
        e02.check(snap1)

        # Simulate a phantom reversal: same id now appears as running.
        c = _conn(canary_db)
        c.execute(
            "UPDATE schedule_executions SET status='running', completed_at=NULL WHERE id='e-done'"
        )
        c.commit()
        c.close()

        snap2 = reload_canary["canary"].collect_snapshot()
        violations = e02.check(snap2)
        assert len(violations) == 1
        v = violations[0]
        assert v.invariant_id == "E-02"
        assert v.observed_state["execution_id"] == "e-done"
        assert v.observed_state["current_status"] == "running"


# ---------------------------------------------------------------------------
# Invariant: L-03 delete cascades — primary smoke test (Option 1)
# ---------------------------------------------------------------------------


class TestInvariantL03:
    def test_holds_with_no_orphans(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import l03_delete_cascades as l03

        assert l03.check(snap) == []

    def test_fires_on_orphan_agent_sharing_row(self, canary_db, reload_canary):
        """Option-1 smoke fixture: insert one orphan row → exactly one L-03."""
        _add_agent(canary_db, "real-agent")
        # Ghost agent has no agent_ownership row.
        _add_orphan_sharing(canary_db, "ghost-canary-zzz")

        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import l03_delete_cascades as l03

        violations = l03.check(snap)
        assert len(violations) == 1, "one orphan agent → one violation report"
        v = violations[0]
        assert v.invariant_id == "L-03"
        assert v.tier == "A"
        # agent_sharing alone is non-active orchestration → major, not critical.
        assert v.severity == "major"
        assert v.observed_state["ghost_agent_name"] == "ghost-canary-zzz"
        assert v.observed_state["orphan_count"] == 1
        assert "agent_sharing" in v.observed_state["tables_hit"]

    def test_critical_severity_for_orphan_running_execution(
        self, canary_db, reload_canary
    ):
        _add_agent(canary_db, "real-agent")
        # Direct INSERT of an execution row pointing at a ghost agent —
        # this is the bug class #129 caught: agent deleted but a running
        # execution row still references it.
        c = _conn(canary_db)
        c.execute(
            "INSERT INTO schedule_executions (id, agent_name, status, started_at) "
            "VALUES ('e-orphan', 'ghost', 'running', '2026-04-30T00:00:00Z')"
        )
        c.commit()
        c.close()

        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import l03_delete_cascades as l03

        violations = l03.check(snap)
        assert len(violations) == 1
        v = violations[0]
        assert v.severity == "critical", "active-orchestration orphan is critical"
        assert "schedule_executions" in v.observed_state["tables_hit"]

    def test_groups_multiple_orphan_rows_under_one_violation(
        self, canary_db, reload_canary
    ):
        _add_agent(canary_db, "real-agent")
        _add_orphan_sharing(canary_db, "ghost-1")
        _add_orphan_sharing(canary_db, "ghost-1")  # second sharing row, same ghost
        _add_orphan_sharing(canary_db, "ghost-2")

        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import l03_delete_cascades as l03

        violations = l03.check(snap)
        # Two ghost agents → two violations regardless of multiple rows per ghost.
        ghost_names = {v.observed_state["ghost_agent_name"] for v in violations}
        assert ghost_names == {"ghost-1", "ghost-2"}

        # And the row count is captured in observed_state.
        ghost1 = next(v for v in violations if v.observed_state["ghost_agent_name"] == "ghost-1")
        assert ghost1.observed_state["orphan_count"] == 2

    def test_redis_orphan_slot_alone_fires_critical(self, canary_db, reload_canary):
        _add_agent(canary_db, "real-agent")
        # Redis slot for ghost agent — no SQL orphan rows.
        reload_canary["redis"].zadd("agent:slots:ghost-redis", {"e-1": 1.0})

        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import l03_delete_cascades as l03

        violations = l03.check(snap)
        assert len(violations) == 1
        v = violations[0]
        assert v.severity == "critical"
        assert v.observed_state["redis_slot_count"] == 1
        assert "redis:agent:slots" in v.observed_state["tables_hit"]


# ---------------------------------------------------------------------------
# Registry / runner
# ---------------------------------------------------------------------------


class TestRunner:
    def test_run_invariants_all(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        _add_orphan_sharing(canary_db, "ghost")

        snap = reload_canary["canary"].collect_snapshot()
        results = reload_canary["canary"].run_invariants(snap)

        assert set(results.keys()) == {"S-01", "E-02", "L-03"}
        assert results["S-01"] == []
        assert results["E-02"] == []
        assert len(results["L-03"]) == 1

    def test_run_invariants_subset(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        _add_orphan_sharing(canary_db, "ghost")

        snap = reload_canary["canary"].collect_snapshot()
        results = reload_canary["canary"].run_invariants(snap, ids=["L-03"])
        assert set(results.keys()) == {"L-03"}

    def test_unknown_id_silently_ignored_by_runner(self, canary_db, reload_canary):
        snap = reload_canary["canary"].collect_snapshot()
        results = reload_canary["canary"].run_invariants(snap, ids=["NOPE", "L-03"])
        assert "NOPE" not in results
        assert "L-03" in results


# ---------------------------------------------------------------------------
# CanaryService.run_cycle orchestration
# ---------------------------------------------------------------------------
#
# These tests exercise the orchestrator that ties snapshot collection,
# invariant evaluation, persistence, and green→red transition detection
# together. The deterministic-library tests above cover individual parts;
# these cover the wiring — which is where the demo-driven bugs lived:
#
#   - e7c11b2e: `_is_green_to_red` was firing on every continuing-red
#     cycle. Fixed via a Redis previous-cycle cursor.
#   - ef40cf98: `TERMINAL_EXECUTION_STATUSES` listed wrong strings
#     ("completed"/"timeout") so E-02's Redis side-table never seeded
#     against real-world `success` rows.
#
# Both bugs passed the unit suite and were caught only by hand-driven
# demo runs. This class is the regression net.


@pytest.fixture
def canary_service(canary_db, fake_redis, reload_canary, monkeypatch):
    """Build a CanaryService bound to the test fixtures.

    Routes db calls through the real `CanaryOperations` (already wired
    to the temp SQLite via `canary_db`) and replaces `db.create_notification`
    with a counting recorder so transitions can be asserted directly.
    """
    db_canary = reload_canary["db_canary"]
    canary_ops = db_canary.CanaryOperations()

    notification_calls: List[Dict[str, Any]] = []

    class _StubNotification:
        # _broadcast is a no-op when both ws managers are None (test default),
        # so we only need a placeholder return value.
        id = "stub-notification-id"
        agent_name = "canary-harness"
        notification_type = "alert"
        title = ""
        priority = "high"
        category = "canary"
        created_at = "2026-04-30T12:00:00Z"

    class _FakeDB:
        def get_latest_canary_violation_per_invariant(self):
            return canary_ops.get_latest_per_invariant()

        def insert_canary_violation(self, **kwargs):
            return canary_ops.insert_violation(**kwargs)

        def create_notification(self, agent_name, data):
            notification_calls.append({"agent_name": agent_name, "data": data})
            return _StubNotification()

    fake_database = types.ModuleType("database")
    fake_database.db = _FakeDB()
    monkeypatch.setitem(sys.modules, "database", fake_database)

    class _NotificationCreate:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    fake_db_models = types.ModuleType("db_models")
    fake_db_models.NotificationCreate = _NotificationCreate
    monkeypatch.setitem(sys.modules, "db_models", fake_db_models)

    # Drop any cached canary_service so it picks up the stubs above.
    sys.modules.pop("services.canary_service", None)

    from services.canary_service import CanaryService

    return {
        "service": CanaryService(),
        "notification_calls": notification_calls,
        "canary_ops": canary_ops,
    }


def _run(coro):
    """Run a coroutine to completion in a fresh event loop."""
    import asyncio as _asyncio
    return _asyncio.run(coro)


class TestCanaryService:
    """End-to-end tests for `CanaryService.run_cycle()`."""

    def test_first_cycle_violation_fires_one_notification(
        self, canary_db, canary_service
    ):
        """First cycle that sees a violation emits exactly one notification."""
        _add_agent(canary_db, "real")
        _add_orphan_sharing(canary_db, "ghost-1")  # triggers L-03

        result = _run(canary_service["service"].run_cycle())

        assert "L-03" in result.transition_invariant_ids
        assert len(canary_service["notification_calls"]) == 1
        call = canary_service["notification_calls"][0]
        assert call["agent_name"] == "canary-harness"
        assert "L-03" in call["data"].title

    def test_continuing_red_does_not_re_fire(self, canary_db, canary_service):
        """Same orphan, three cycles → 3 violations persisted, 1 notification.

        Regression for e7c11b2e: transition detection was firing on every
        continuing-red cycle. The fix uses a Redis previous-cycle cursor
        so a continuously-red invariant rings the bell exactly once.
        """
        _add_agent(canary_db, "real")
        _add_orphan_sharing(canary_db, "ghost-1")

        svc = canary_service["service"]
        _run(svc.run_cycle())
        _run(svc.run_cycle())
        _run(svc.run_cycle())

        # All three cycles still persist the violation — the forensic
        # record is intact even when the bell stays quiet.
        ops = canary_service["canary_ops"]
        assert ops.count_violations(invariant_id="L-03") == 3
        assert len(canary_service["notification_calls"]) == 1, (
            "continuing-red must not re-notify on every cycle"
        )

    def test_red_green_red_fires_twice(self, canary_db, canary_service):
        """red → green → red emits two notifications.

        A clean cycle in the middle "re-arms" the invariant; the next
        violation is a fresh transition, not a continuation.
        """
        _add_agent(canary_db, "real")
        _add_orphan_sharing(canary_db, "ghost-1")

        svc = canary_service["service"]

        # Cycle 1: red.
        _run(svc.run_cycle())
        assert len(canary_service["notification_calls"]) == 1

        # Cycle 2: clean it up → green.
        c = _conn(canary_db)
        c.execute("DELETE FROM agent_sharing WHERE agent_name='ghost-1'")
        c.commit()
        c.close()
        _run(svc.run_cycle())
        assert len(canary_service["notification_calls"]) == 1, (
            "green cycle must not emit"
        )

        # Cycle 3: re-introduce → red again.
        _add_orphan_sharing(canary_db, "ghost-1")
        _run(svc.run_cycle())

        assert len(canary_service["notification_calls"]) == 2, (
            "red→green→red must fire on the second red transition"
        )

    def test_terminal_status_set_seeds_e02_side_table(
        self, canary_db, canary_service, fake_redis
    ):
        """Regression for ef40cf98 — the terminal-status-set typo.

        `TERMINAL_EXECUTION_STATUSES` previously listed
        ("completed", "failed", "cancelled", "timeout"), but Trinity
        actually writes ("success", "failed", "cancelled", "skipped").
        With the wrong list, a `success` row never made it into
        `canary:e02:terminal_seen`, so a later reversal of the same id
        would go undetected. This test fails against the pre-fix list.
        """
        _add_agent(canary_db, "real")
        _add_execution(
            canary_db,
            "e-real-success",
            "real",
            "success",
            completed_at=datetime.utcnow().isoformat(),
        )

        _run(canary_service["service"].run_cycle())

        terminal_seen = fake_redis.hkeys("canary:e02:terminal_seen")
        assert "e-real-success" in terminal_seen, (
            "'success' must be in TERMINAL_EXECUTION_STATUSES"
        )
