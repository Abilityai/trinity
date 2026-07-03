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


# Stub `models.TaskExecutionStatus` so canary/snapshot.py can derive its
# terminal-status tuple from the canonical enum without dragging the
# real `models` module (and its pydantic / db_models dependencies) into
# unit-test imports. Mirrors the four terminal values from
# src/backend/models.py:TaskExecutionStatus.
if "models" not in sys.modules:
    from enum import Enum as _Enum

    class _StubTaskExecutionStatus(str, _Enum):
        SUCCESS = "success"
        FAILED = "failed"
        CANCELLED = "cancelled"
        SKIPPED = "skipped"
        # Non-terminal values still listed so tests that touch them
        # match the real enum's surface area.
        QUEUED = "queued"
        RUNNING = "running"
        PENDING_RETRY = "pending_retry"

    _models_mod = types.ModuleType("models")
    _models_mod.TaskExecutionStatus = _StubTaskExecutionStatus
    sys.modules["models"] = _models_mod


# ---------------------------------------------------------------------------
# Tiny in-memory Redis substitute — covers only the surface canary uses.
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal Redis stand-in for canary tests (ZSET + HASH + SCAN)."""

    def __init__(self):
        self._zsets: Dict[str, Dict[str, float]] = defaultdict(dict)
        self._hashes: Dict[str, Dict[str, str]] = defaultdict(dict)
        self._strings: Dict[str, str] = {}
        # Per-key TTL (seconds). Test-controlled: tests inject values via
        # `set_ttl(key, ttl)` to mimic the three redis.ttl() return cases.
        # See S-03 invariant for the sentinel values (-2 / -1 / >0).
        self._ttls: Dict[str, int] = {}

    # ZSET ------------------------------------------------------------------

    def zadd(self, key: str, mapping: Dict[str, float]) -> int:
        added = 0
        for member, score in mapping.items():
            if member not in self._zsets[key]:
                added += 1
            self._zsets[key][member] = score
        return added

    def zrange(self, key: str, start: int, end: int, withscores: bool = False):
        items = sorted(self._zsets.get(key, {}).items(), key=lambda kv: kv[1])
        sliced = items[start:] if end == -1 else items[start : end + 1]
        return list(sliced) if withscores else [m for m, _ in sliced]

    def zcard(self, key: str) -> int:
        return len(self._zsets.get(key, {}))

    def zrem(self, key: str, member: str) -> int:
        if member in self._zsets.get(key, {}):
            del self._zsets[key][member]
            return 1
        return 0

    def zremrangebyscore(self, key: str, min_score, max_score) -> int:
        # Accepts numerics or the strings "-inf" / "+inf" — same as redis-py.
        def _coerce(v):
            if isinstance(v, str):
                if v in ("-inf", "inf", "+inf"):
                    return float(v)
            return float(v)

        lo = _coerce(min_score)
        hi = _coerce(max_score)
        if key not in self._zsets:
            return 0
        to_remove = [m for m, s in self._zsets[key].items() if lo <= s <= hi]
        for m in to_remove:
            del self._zsets[key][m]
        return len(to_remove)

    def zrangebyscore(self, key: str, min_score, max_score) -> List[str]:
        def _coerce(v):
            if isinstance(v, str) and v in ("-inf", "inf", "+inf"):
                return float(v)
            return float(v)

        lo = _coerce(min_score)
        hi = _coerce(max_score)
        items = sorted(
            (kv for kv in self._zsets.get(key, {}).items() if lo <= kv[1] <= hi),
            key=lambda kv: kv[1],
        )
        return [m for m, _ in items]

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

    def hmget(self, key: str, *fields: str) -> List:
        bucket = self._hashes.get(key, {})
        return [bucket.get(f) for f in fields]

    def hdel(self, key: str, *fields: str) -> int:
        bucket = self._hashes.get(key)
        if not bucket:
            return 0
        removed = 0
        for f in fields:
            if f in bucket:
                del bucket[f]
                removed += 1
        return removed

    def hkeys(self, key: str) -> List[str]:
        return list(self._hashes.get(key, {}).keys())

    def hlen(self, key: str) -> int:
        return len(self._hashes.get(key, {}))

    # TTL -------------------------------------------------------------------
    # Matches redis-py semantics: positive int = seconds remaining,
    # -2 = key does not exist, -1 = key exists but no TTL.
    # Tests set values via `set_ttl()`.

    def ttl(self, key: str) -> int:
        if key in self._ttls:
            return self._ttls[key]
        if key in self._hashes or key in self._zsets or key in self._strings:
            return -1  # exists but no TTL
        return -2  # missing

    def set_ttl(self, key: str, value: int) -> None:
        self._ttls[key] = value

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
            execution_timeout_seconds INTEGER DEFAULT 900,
            deleted_at TEXT  -- #834: read paths filter `WHERE deleted_at IS NULL`
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

    # Route the SQLAlchemy engine seam (#300) at the SAME temp file. Converted
    # db modules (e.g. CanaryOperations) use get_engine(), whose cache is keyed
    # by URL — dispose so the temp file's engine is created, and dispose again
    # at teardown so the cached engine is dropped.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    import db.engine as engine_mod
    engine_mod.dispose_engines()

    yield db_path

    engine_mod.dispose_engines()
    os.unlink(db_path)


@pytest.fixture
def fake_redis(monkeypatch):
    """Patch services.slot_service.get_slot_service to a fake."""
    redis_inst = FakeRedis()

    class _FakeSlotService:
        slots_prefix = "agent:slots:"
        metadata_prefix = "agent:slot:"

        def __init__(self):
            self.redis = redis_inst

    fake_module = types.ModuleType("services.slot_service")
    fake_module.get_slot_service = lambda: _FakeSlotService()
    monkeypatch.setitem(sys.modules, "services.slot_service", fake_module)

    return redis_inst


@pytest.fixture
def fake_docker(monkeypatch):
    """Stub services.docker_service with a controllable container list.

    Phase 3's R-01 snapshot collector reads `docker_client.containers.list`
    and calls `container.exec_run` per match. Without a stub, the canary
    snapshot records `docker: client unavailable` in
    `sources_unavailable` and unrelated tests assert on that being empty.
    Tests that exercise R-01 manipulate `fake_docker._containers` directly.
    """

    class _FakeContainer:
        def __init__(self, name, exec_output="0", exec_raises=None):
            self.name = name
            self._exec_output = exec_output
            self._exec_raises = exec_raises

        def exec_run(self, cmd):
            if self._exec_raises is not None:
                raise self._exec_raises
            # Mimic the docker-py ExecResult shape used by the collector.
            class _R:
                pass
            r = _R()
            r.exit_code = 0
            r.output = self._exec_output.encode("utf-8")
            return r

    class _FakeContainers:
        def __init__(self):
            self._items = []

        def list(self, filters=None):
            return list(self._items)

    class _FakeDockerClient:
        def __init__(self):
            self.containers = _FakeContainers()

    client = _FakeDockerClient()

    fake_module = types.ModuleType("services.docker_service")
    fake_module.docker_client = client
    # Stubs for the names services/__init__.py re-exports; canary doesn't
    # call these, but the canary_service tests transitively import
    # services/__init__.py and would fail with AttributeError otherwise.
    fake_module.get_agent_container = lambda *a, **kw: None
    fake_module.get_agent_status_from_container = lambda *a, **kw: None
    fake_module.list_all_agents = lambda *a, **kw: []
    fake_module.get_agent_by_name = lambda *a, **kw: None
    fake_module.get_next_available_port = lambda *a, **kw: 2222
    monkeypatch.setitem(sys.modules, "services.docker_service", fake_module)

    # Expose the containers list + factory so tests can populate easily.
    client.add_container = lambda *a, **kw: client.containers._items.append(
        _FakeContainer(*a, **kw)
    )
    return client


@pytest.fixture
def reload_canary(canary_db, fake_redis, fake_docker):
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
        assert "e-recent" in snap.terminal_exec_statuses
        assert snap.terminal_exec_statuses["e-recent"] == "success"
        assert "e-old" not in snap.terminal_exec_statuses


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
        # #1082: downgraded critical → major (redundant under single-owner
        # status; still Tier A, still fires on real ZSET/SQL drift).
        assert v.severity == "major"
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

    def test_grace_suppresses_fresh_sql_orphan(self, canary_db, reload_canary):
        """Start-path race: SQL row freshly written, ZADD not landed yet."""
        import time
        _add_agent(canary_db, "a1")
        fresh = datetime.utcfromtimestamp(time.time()).isoformat() + "Z"
        _add_execution(canary_db, "e-fresh", "a1", "running", started_at=fresh)

        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s01_slot_row_bijection as s01

        assert s01.check(snap) == []

    def test_grace_suppresses_fresh_redis_phantom(self, canary_db, reload_canary):
        """Stop-path race: ZSET score within grace, SQL already terminal."""
        import time
        _add_agent(canary_db, "a1")
        reload_canary["redis"].zadd("agent:slots:a1", {"e-fresh": time.time()})

        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s01_slot_row_bijection as s01

        assert s01.check(snap) == []

    def test_grace_does_not_suppress_durable_mismatch(self, canary_db, reload_canary):
        """Old `started_at` + old ZSET score → real leak, must fire."""
        _add_agent(canary_db, "a1")
        _add_execution(canary_db, "e-stale-sql", "a1", "running")  # default 2026-04-30
        reload_canary["redis"].zadd("agent:slots:a1", {"e-stale-redis": 1.0})  # 1970

        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s01_slot_row_bijection as s01

        violations = s01.check(snap)
        assert len(violations) == 1
        obs = violations[0].observed_state
        assert obs["in_sql_only"] == ["e-stale-sql"]
        assert obs["in_redis_only"] == ["e-stale-redis"]


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
        # Forensic value of the alert: the reversal report must carry the
        # actual prior status (success / failed / cancelled / skipped),
        # not the placeholder string "terminal" the early Phase 1 cut
        # used to write into the side-table. The Slack renderer prints
        # this verbatim — "terminal → running" is useless to on-call.
        assert v.observed_state["previous_status"] == "success"
        assert v.signal_query and "success" in v.signal_query

    def test_reversal_renders_real_prior_status_for_each_terminal_kind(
        self, canary_db, reload_canary
    ):
        """The four terminal statuses round-trip through the side-table.

        Regression for the placeholder-string bug: the previous-cycle
        side-table only carried the literal "terminal" string, so a
        reversal of e.g. a `cancelled` row reported "terminal → running"
        instead of "cancelled → running". Run all four through the
        seed/reverse cycle and assert each comes back labelled correctly.
        """
        from canary.invariants import e02_no_phantom_reversal as e02

        # Seed cycle: one row per terminal status.
        _add_agent(canary_db, "a1")
        for eid, status in (
            ("e-success", "success"),
            ("e-failed", "failed"),
            ("e-cancelled", "cancelled"),
            ("e-skipped", "skipped"),
        ):
            _add_execution(
                canary_db, eid, "a1", status,
                completed_at=datetime.utcnow().isoformat(),
            )
        snap1 = reload_canary["canary"].collect_snapshot()
        e02.check(snap1)

        # Reversal: flip them all to running.
        c = _conn(canary_db)
        c.execute(
            "UPDATE schedule_executions SET status='running', completed_at=NULL"
        )
        c.commit()
        c.close()

        snap2 = reload_canary["canary"].collect_snapshot()
        violations = e02.check(snap2)
        prev_by_eid = {
            v.observed_state["execution_id"]: v.observed_state["previous_status"]
            for v in violations
        }
        assert prev_by_eid == {
            "e-success": "success",
            "e-failed": "failed",
            "e-cancelled": "cancelled",
            "e-skipped": "skipped",
        }

    def test_side_table_trims_by_age_not_by_hard_reset(
        self, canary_db, reload_canary, fake_redis
    ):
        """Aged-out ids are dropped; in-window ids survive.

        Regression for the pre-fix hard-reset trim: when the side-table
        crossed a 5000-entry hash cap the entire key was DEL'd, leaving
        a one-cycle E-02 blind spot. The fix uses a sorted set scored
        by unix ts and trims via `ZREMRANGEBYSCORE`, so only entries
        older than the retention window age out — never an in-window
        terminal id. Verifies that property directly.
        """
        from canary.invariants import e02_no_phantom_reversal as e02

        # Seed: one stale entry (well past retention) and one fresh.
        # Use scores < cutoff and > cutoff to test the boundary.
        retention = e02.PREV_TERMINAL_RETENTION_SECONDS
        import time as _time
        now = _time.time()
        fake_redis.zadd(
            e02.REDIS_KEY_PREV_TERMINAL,
            {
                "stale-eid": now - retention - 60,    # past cutoff
                "fresh-eid": now - 30,                # well inside window
            },
        )

        # Run one check; both pre-existing ids are non-running, so no
        # violation, but the trim path should drop "stale-eid" and keep
        # "fresh-eid".
        _add_agent(canary_db, "a1")
        snap = reload_canary["canary"].collect_snapshot()
        e02.check(snap)

        survivors = set(
            fake_redis.zrange(e02.REDIS_KEY_PREV_TERMINAL, 0, -1)
        )
        assert "stale-eid" not in survivors, "aged-out id must be trimmed"
        assert "fresh-eid" in survivors, (
            "in-window id must NOT be lost to a hard reset"
        )


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

        assert set(results.keys()) == {
            "S-01", "S-02", "S-03",
            "E-01", "E-02", "E-05",
            "L-03",
            "B-01", "B-02",
            "R-01",
        }
        assert results["S-01"] == []
        assert results["S-02"] == []
        assert results["S-03"] == []
        assert results["E-01"] == []
        assert results["E-02"] == []
        assert results["E-05"] == []
        assert len(results["L-03"]) == 1
        # B-01 is skipped in unit-test mode (no live `database` facade), so
        # queued_count_via_service is None per agent → no violations.
        assert results["B-01"] == []
        # B-02 / R-01 are green on a clean platform.
        assert results["B-02"] == []
        assert results["R-01"] == []

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
# S-02 — no overbooking
# ---------------------------------------------------------------------------


class TestInvariantS02:
    def test_holds_when_slot_count_within_cap(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1", max_parallel=3)
        reload_canary["redis"].zadd(
            "agent:slots:a1", {"e1": 1.0, "e2": 2.0, "e3": 3.0}
        )
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s02_no_overbooking as s02
        assert s02.check(snap) == []

    def test_fires_when_slot_count_exceeds_cap(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1", max_parallel=2)
        reload_canary["redis"].zadd(
            "agent:slots:a1", {"e1": 1.0, "e2": 2.0, "e3": 3.0}
        )
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s02_no_overbooking as s02
        violations = s02.check(snap)
        assert len(violations) == 1
        v = violations[0]
        assert v.invariant_id == "S-02"
        assert v.severity == "critical"
        assert v.observed_state["slot_count"] == 3
        assert v.observed_state["max_parallel_tasks"] == 2
        assert v.observed_state["overbooked_by"] == 1

    def test_drain_sentinels_filtered_before_cap_check(
        self, canary_db, reload_canary
    ):
        """Drain sentinels briefly push ZCARD over the cap; not a violation."""
        _add_agent(canary_db, "a1", max_parallel=2)
        reload_canary["redis"].zadd(
            "agent:slots:a1",
            {"e1": 1.0, "e2": 2.0, "drain-a1-1234567890.5": 3.0},
        )
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s02_no_overbooking as s02
        assert s02.check(snap) == []

    def test_skipped_when_redis_unavailable(self, canary_db, reload_canary):
        from canary.snapshot import Snapshot, AgentSnapshot
        snap = Snapshot(
            snapshot_time="2026-05-18T12:00:00Z",
            sources_unavailable=["redis: connection refused"],
            agents=[
                AgentSnapshot(
                    name="a1",
                    is_system=False,
                    max_parallel=1,
                    execution_timeout_seconds=900,
                    slot_ids={"e1", "e2", "e3"},
                )
            ],
        )
        from canary.invariants import s02_no_overbooking as s02
        assert s02.check(snap) == []


# ---------------------------------------------------------------------------
# E-01 — terminal-state closure
# ---------------------------------------------------------------------------


class TestInvariantE01:
    @staticmethod
    def _snap(
        *,
        snap_time="2026-05-18T12:00:00Z",
        started_at="2026-05-18T11:00:00Z",
        timeout=900,
        running_ids=("e1",),
    ):
        from canary.snapshot import Snapshot, AgentSnapshot
        return Snapshot(
            snapshot_time=snap_time,
            agents=[
                AgentSnapshot(
                    name="a1",
                    is_system=False,
                    max_parallel=3,
                    execution_timeout_seconds=timeout,
                    running_exec_ids=set(running_ids),
                    running_started_at={eid: started_at for eid in running_ids},
                )
            ],
        )

    def test_holds_when_running_row_within_window(self):
        # Started 100s ago, timeout 900s + buffer 300s = 1200s window.
        snap = self._snap(
            snap_time="2026-05-18T12:01:40Z",
            started_at="2026-05-18T12:00:00Z",
        )
        from canary.invariants import e01_terminal_state_closure as e01
        assert e01.check(snap) == []

    def test_fires_when_running_row_past_timeout_plus_buffer(self):
        # Started 1 hour ago, timeout 900s + buffer 300s = 1200s window.
        # 3600s > 1200s → violation.
        snap = self._snap(
            snap_time="2026-05-18T12:00:00Z",
            started_at="2026-05-18T11:00:00Z",
            timeout=900,
        )
        from canary.invariants import e01_terminal_state_closure as e01
        violations = e01.check(snap)
        assert len(violations) == 1
        v = violations[0]
        assert v.invariant_id == "E-01"
        assert v.severity == "critical"
        assert v.observed_state["age_seconds"] == 3600
        assert v.observed_state["execution_timeout_seconds"] == 900

    def test_skips_row_without_started_at(self):
        from canary.snapshot import Snapshot, AgentSnapshot
        snap = Snapshot(
            snapshot_time="2026-05-18T12:00:00Z",
            agents=[
                AgentSnapshot(
                    name="a1",
                    is_system=False,
                    max_parallel=3,
                    execution_timeout_seconds=900,
                    running_exec_ids={"e1"},
                    running_started_at={},
                )
            ],
        )
        from canary.invariants import e01_terminal_state_closure as e01
        assert e01.check(snap) == []

    def test_skips_row_with_malformed_started_at(self):
        snap = self._snap(started_at="not-an-iso-timestamp")
        from canary.invariants import e01_terminal_state_closure as e01
        assert e01.check(snap) == []


# ---------------------------------------------------------------------------
# E-05 — dispatched rows have session
# ---------------------------------------------------------------------------


class TestInvariantE05:
    @staticmethod
    def _snap(*, started_at, session_id):
        from canary.snapshot import Snapshot, AgentSnapshot
        return Snapshot(
            snapshot_time="2026-05-18T12:00:00Z",
            agents=[
                AgentSnapshot(
                    name="a1",
                    is_system=False,
                    max_parallel=3,
                    execution_timeout_seconds=900,
                    running_exec_ids={"e1"},
                    running_started_at={"e1": started_at},
                    running_claude_session_ids={"e1": session_id},
                )
            ],
        )

    def test_holds_when_session_id_present(self):
        # Old row but with a session — fine.
        snap = self._snap(
            started_at="2026-05-18T11:00:00Z",
            session_id="abc-session-uuid",
        )
        from canary.invariants import e05_dispatched_rows_have_session as e05
        assert e05.check(snap) == []

    def test_holds_when_row_within_grace_and_no_session(self):
        # 30s old, grace is 60s → still in grace.
        snap = self._snap(
            started_at="2026-05-18T11:59:30Z",
            session_id=None,
        )
        from canary.invariants import e05_dispatched_rows_have_session as e05
        assert e05.check(snap) == []

    def test_fires_when_old_row_lacks_session(self):
        # 1 hour old with no session — fires.
        snap = self._snap(
            started_at="2026-05-18T11:00:00Z",
            session_id=None,
        )
        from canary.invariants import e05_dispatched_rows_have_session as e05
        violations = e05.check(snap)
        assert len(violations) == 1
        v = violations[0]
        assert v.invariant_id == "E-05"
        assert v.severity == "major"
        assert v.observed_state["age_seconds"] == 3600


# ---------------------------------------------------------------------------
# B-01 — queue-status coherence
# ---------------------------------------------------------------------------


class TestInvariantB01:
    @staticmethod
    def _snap(*, queued_ids, service_count):
        from canary.snapshot import Snapshot, AgentSnapshot
        return Snapshot(
            snapshot_time="2026-05-18T12:00:00Z",
            agents=[
                AgentSnapshot(
                    name="a1",
                    is_system=False,
                    max_parallel=3,
                    execution_timeout_seconds=900,
                    queued_exec_ids=set(queued_ids),
                    queued_count_via_service=service_count,
                )
            ],
        )

    def test_holds_when_counts_agree(self):
        snap = self._snap(queued_ids={"q1", "q2"}, service_count=2)
        from canary.invariants import b01_queue_status_coherence as b01
        assert b01.check(snap) == []

    def test_holds_when_both_zero(self):
        snap = self._snap(queued_ids=set(), service_count=0)
        from canary.invariants import b01_queue_status_coherence as b01
        assert b01.check(snap) == []

    def test_fires_when_service_undercounts(self):
        snap = self._snap(queued_ids={"q1", "q2", "q3"}, service_count=1)
        from canary.invariants import b01_queue_status_coherence as b01
        violations = b01.check(snap)
        assert len(violations) == 1
        v = violations[0]
        assert v.invariant_id == "B-01"
        assert v.severity == "critical"
        assert v.observed_state["service_count"] == 1
        assert v.observed_state["snapshot_count"] == 3

    def test_fires_when_service_overcounts(self):
        snap = self._snap(queued_ids={"q1"}, service_count=5)
        from canary.invariants import b01_queue_status_coherence as b01
        violations = b01.check(snap)
        assert len(violations) == 1
        assert violations[0].observed_state["service_count"] == 5
        assert violations[0].observed_state["snapshot_count"] == 1

    def test_skips_when_service_count_none(self):
        """Snapshot built without the `database` facade reachable."""
        snap = self._snap(queued_ids={"q1"}, service_count=None)
        from canary.invariants import b01_queue_status_coherence as b01
        assert b01.check(snap) == []


# ---------------------------------------------------------------------------
# S-03 — slot TTL floor
# ---------------------------------------------------------------------------


class TestInvariantS03:
    # ZSET score must look like a real acquire-time unix epoch, not 1.0
    # (year 1970), because S-03 reconstructs the slot's initial TTL via
    # `ttl + (snapshot_time - score)` after #913 made the natural
    # 1-second TTL decay against the floor a false positive.
    @staticmethod
    def _now_score() -> float:
        import time
        return time.time()

    def test_holds_when_ttl_above_floor(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1", timeout=60)  # floor = 60 + 300 = 360s
        reload_canary["redis"].zadd("agent:slots:a1", {"e1": self._now_score()})
        reload_canary["redis"].set_ttl("agent:slot:a1:e1", 500)
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s03_slot_ttl_floor as s03
        assert s03.check(snap) == []

    def test_fires_below_floor(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1", timeout=60)  # floor = 360s
        # Slot acquired ~now with EXPIRE=100. Initial TTL = 100 + ~0 age
        # = 100 < 360 floor — fires.
        reload_canary["redis"].zadd("agent:slots:a1", {"e1": self._now_score()})
        reload_canary["redis"].set_ttl("agent:slot:a1:e1", 100)
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s03_slot_ttl_floor as s03
        v = s03.check(snap)
        assert len(v) == 1
        assert v[0].invariant_id == "S-03"
        assert v[0].severity == "critical"
        assert v[0].observed_state["kind"] == "below_floor"
        assert v[0].observed_state["redis_ttl_seconds"] == 100
        assert v[0].observed_state["floor_seconds"] == 360

    def test_natural_decay_not_below_floor(self, canary_db, reload_canary):
        """#913 regression — a slot created with EXPIRE=floor and observed
        seconds later must not fire just because of natural TTL decay."""
        import time
        _add_agent(canary_db, "a1", timeout=60)  # floor = 360s
        # Slot was created 5s ago with the canonical EXPIRE=floor. Real
        # TTL now reads `360 - 5 = 355` — below the raw floor — but the
        # reconstructed initial TTL is 360, exactly at the floor.
        reload_canary["redis"].zadd("agent:slots:a1", {"e1": time.time() - 5})
        reload_canary["redis"].set_ttl("agent:slot:a1:e1", 355)
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s03_slot_ttl_floor as s03
        assert s03.check(snap) == []

    def test_fires_when_metadata_missing(self, canary_db, reload_canary):
        """ZSET points at a slot whose metadata HASH already expired (#226)."""
        _add_agent(canary_db, "a1", timeout=60)
        reload_canary["redis"].zadd("agent:slots:a1", {"e1": self._now_score()})
        # FakeRedis.ttl returns -2 when neither the hash nor the ttl is set.
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s03_slot_ttl_floor as s03
        v = s03.check(snap)
        assert len(v) == 1
        assert v[0].observed_state["kind"] == "missing"
        assert v[0].observed_state["redis_ttl_seconds"] == -2

    def test_fires_when_ttl_unset(self, canary_db, reload_canary):
        """Metadata HASH exists but no expire was set on it."""
        _add_agent(canary_db, "a1", timeout=60)
        reload_canary["redis"].zadd("agent:slots:a1", {"e1": self._now_score()})
        # Populate the HASH so FakeRedis.ttl returns -1 (exists, no TTL).
        reload_canary["redis"].hset("agent:slot:a1:e1", "started_at", "x")
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s03_slot_ttl_floor as s03
        v = s03.check(snap)
        assert len(v) == 1
        assert v[0].observed_state["kind"] == "no_expiry"
        assert v[0].observed_state["redis_ttl_seconds"] == -1

    def test_drain_sentinels_skipped(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1", timeout=60)
        reload_canary["redis"].zadd(
            "agent:slots:a1", {"drain-a1-12345": self._now_score()}
        )
        # Don't set TTL — would normally be "missing"; sentinel must skip.
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s03_slot_ttl_floor as s03
        assert s03.check(snap) == []

    def test_skipped_when_redis_unavailable(self):
        from canary.snapshot import Snapshot, AgentSnapshot
        snap = Snapshot(
            snapshot_time="2026-05-18T12:00:00Z",
            sources_unavailable=["redis: connection refused"],
            agents=[
                AgentSnapshot(
                    name="a1",
                    is_system=False,
                    max_parallel=3,
                    execution_timeout_seconds=60,
                    slot_ids={"e1"},
                    slot_ttls={"e1": -2},
                )
            ],
        )
        from canary.invariants import s03_slot_ttl_floor as s03
        assert s03.check(snap) == []


# ---------------------------------------------------------------------------
# B-02 — no queued without slots-full
# ---------------------------------------------------------------------------


class TestInvariantB02:
    @staticmethod
    def _snap(*, queued_count, slot_count, max_parallel, drain_tick_at, snap_unix):
        from canary.snapshot import Snapshot, AgentSnapshot
        from datetime import datetime
        snap_time = datetime.utcfromtimestamp(snap_unix).isoformat() + "Z"
        return Snapshot(
            snapshot_time=snap_time,
            drain_tick_at=drain_tick_at,
            agents=[
                AgentSnapshot(
                    name="a1",
                    is_system=False,
                    max_parallel=max_parallel,
                    execution_timeout_seconds=900,
                    slot_ids={f"r{i}" for i in range(slot_count)},
                    queued_exec_ids={f"q{i}" for i in range(queued_count)},
                )
            ],
        )

    def test_holds_when_no_queued(self):
        import time
        snap = self._snap(
            queued_count=0, slot_count=0, max_parallel=3,
            drain_tick_at=None, snap_unix=time.time(),
        )
        from canary.invariants import b02_no_queued_without_slots_full as b02
        assert b02.check(snap) == []

    def test_holds_when_slots_full(self):
        """Queued > 0 is correct when capacity is saturated."""
        import time
        snap = self._snap(
            queued_count=2, slot_count=3, max_parallel=3,
            drain_tick_at=None, snap_unix=time.time(),
        )
        from canary.invariants import b02_no_queued_without_slots_full as b02
        assert b02.check(snap) == []

    def test_holds_when_drain_tick_fresh(self):
        """Free slots + queue, but maintenance fired within 60s — wait."""
        import time
        now = time.time()
        snap = self._snap(
            queued_count=2, slot_count=1, max_parallel=3,
            drain_tick_at=now - 30,  # 30s ago, within grace
            snap_unix=now,
        )
        from canary.invariants import b02_no_queued_without_slots_full as b02
        assert b02.check(snap) == []

    def test_fires_when_drain_tick_stale(self):
        """Free slots + queue + drain tick > 60s old → stuck drain."""
        import time
        now = time.time()
        snap = self._snap(
            queued_count=2, slot_count=1, max_parallel=3,
            drain_tick_at=now - 600,  # 10min ago
            snap_unix=now,
        )
        from canary.invariants import b02_no_queued_without_slots_full as b02
        v = b02.check(snap)
        assert len(v) == 1
        assert v[0].invariant_id == "B-02"
        assert v[0].severity == "critical"
        assert v[0].observed_state["free_slots"] == 2
        assert v[0].observed_state["drain_tick_age_seconds"] == 600

    def test_fires_when_drain_tick_never(self):
        """Heartbeat key absent (cold cluster / write failure)."""
        import time
        snap = self._snap(
            queued_count=1, slot_count=0, max_parallel=3,
            drain_tick_at=None,
            snap_unix=time.time(),
        )
        from canary.invariants import b02_no_queued_without_slots_full as b02
        v = b02.check(snap)
        assert len(v) == 1
        assert v[0].observed_state["drain_tick_age_seconds"] is None

    def test_drain_sentinels_dont_count_as_real_slots(self):
        """Sentinel-held slot doesn't satisfy the slots-full arm."""
        import time
        from canary.snapshot import Snapshot, AgentSnapshot
        from datetime import datetime
        now = time.time()
        snap = Snapshot(
            snapshot_time=datetime.utcfromtimestamp(now).isoformat() + "Z",
            drain_tick_at=now - 600,
            agents=[
                AgentSnapshot(
                    name="a1",
                    is_system=False,
                    max_parallel=1,
                    execution_timeout_seconds=900,
                    # 1 drain sentinel and 0 real slots; cap is 1; queued exists.
                    slot_ids={"drain-a1-99"},
                    queued_exec_ids={"q1"},
                )
            ],
        )
        from canary.invariants import b02_no_queued_without_slots_full as b02
        v = b02.check(snap)
        assert len(v) == 1, "sentinel must not satisfy slots-full arm"

    def test_effective_full_under_lowered_ceiling_does_not_fire(self):
        """#506: stored=5 but ceiling clamps effective=2; with 2 real slots and
        a queue, the agent is effective-full → no violation despite stale drain.

        Without the effective-cap fix this would false-fire ("free slots =
        5 - 2 = 3 → drain stalled").
        """
        import time
        from canary.snapshot import Snapshot, AgentSnapshot
        from datetime import datetime
        now = time.time()
        snap = Snapshot(
            snapshot_time=datetime.utcfromtimestamp(now).isoformat() + "Z",
            drain_tick_at=now - 600,  # stale
            agents=[
                AgentSnapshot(
                    name="a1",
                    is_system=False,
                    max_parallel=5,            # stored, unchanged
                    effective_max_parallel=2,  # clamped to fleet ceiling
                    execution_timeout_seconds=900,
                    slot_ids={"r0", "r1"},     # effective-full
                    queued_exec_ids={"q0", "q1"},
                )
            ],
        )
        from canary.invariants import b02_no_queued_without_slots_full as b02
        assert b02.check(snap) == []

    def test_effective_cap_still_fires_when_genuinely_free(self):
        """#506: stored=5, effective=4, only 2 real slots → genuinely free under
        the effective cap → still a violation when the drain tick is stale.
        """
        import time
        from canary.snapshot import Snapshot, AgentSnapshot
        from datetime import datetime
        now = time.time()
        snap = Snapshot(
            snapshot_time=datetime.utcfromtimestamp(now).isoformat() + "Z",
            drain_tick_at=now - 600,
            agents=[
                AgentSnapshot(
                    name="a1",
                    is_system=False,
                    max_parallel=5,
                    effective_max_parallel=4,
                    execution_timeout_seconds=900,
                    slot_ids={"r0", "r1"},
                    queued_exec_ids={"q0"},
                )
            ],
        )
        from canary.invariants import b02_no_queued_without_slots_full as b02
        v = b02.check(snap)
        assert len(v) == 1
        assert v[0].observed_state["effective_max_parallel_tasks"] == 4
        assert v[0].observed_state["free_slots"] == 2  # 4 - 2

    def test_skipped_when_redis_unavailable(self):
        from canary.snapshot import Snapshot, AgentSnapshot
        snap = Snapshot(
            snapshot_time="2026-05-18T12:00:00Z",
            sources_unavailable=["redis: down"],
            drain_tick_at=None,
            agents=[
                AgentSnapshot(
                    name="a1",
                    is_system=False,
                    max_parallel=3,
                    execution_timeout_seconds=900,
                    slot_ids=set(),
                    queued_exec_ids={"q1"},
                )
            ],
        )
        from canary.invariants import b02_no_queued_without_slots_full as b02
        assert b02.check(snap) == []


# ---------------------------------------------------------------------------
# R-01 — no zombie claude processes
# ---------------------------------------------------------------------------


class TestInvariantR01:
    def test_holds_when_no_zombies(self, canary_db, reload_canary, fake_docker):
        _add_agent(canary_db, "a1")
        fake_docker.add_container("agent-a1", exec_output="0")
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import r01_no_zombie_claude as r01
        assert r01.check(snap) == []
        assert snap.zombie_counts == {"a1": 0}

    def test_fires_on_zombie_count(self, canary_db, reload_canary, fake_docker):
        _add_agent(canary_db, "a1")
        fake_docker.add_container("agent-a1", exec_output="3")
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import r01_no_zombie_claude as r01
        v = r01.check(snap)
        assert len(v) == 1
        assert v[0].invariant_id == "R-01"
        assert v[0].severity == "critical"
        assert v[0].observed_state["agent_name"] == "a1"
        assert v[0].observed_state["zombie_count"] == 3

    def test_per_container_exec_failure_does_not_kill_cycle(
        self, canary_db, reload_canary, fake_docker
    ):
        _add_agent(canary_db, "ok")
        _add_agent(canary_db, "broken")
        fake_docker.add_container("agent-ok", exec_output="0")
        fake_docker.add_container("agent-broken", exec_raises=RuntimeError("boom"))
        snap = reload_canary["canary"].collect_snapshot()
        # The healthy container is still measured; the broken one is in
        # sources_unavailable. Neither agent fires R-01.
        assert snap.zombie_counts == {"ok": 0}
        assert any("docker.exec[broken]" in s for s in snap.sources_unavailable)
        from canary.invariants import r01_no_zombie_claude as r01
        assert r01.check(snap) == []

    def test_silent_when_docker_unavailable(self, canary_db, reload_canary, monkeypatch):
        """All-or-nothing docker failure — R-01 produces no violations."""
        # Override the existing docker stub so docker_client is None.
        fake_module = types.ModuleType("services.docker_service")
        fake_module.docker_client = None
        fake_module.get_agent_container = lambda *a, **kw: None
        fake_module.get_agent_status_from_container = lambda *a, **kw: None
        fake_module.list_all_agents = lambda *a, **kw: []
        fake_module.get_agent_by_name = lambda *a, **kw: None
        fake_module.get_next_available_port = lambda *a, **kw: 2222
        monkeypatch.setitem(sys.modules, "services.docker_service", fake_module)
        _add_agent(canary_db, "a1")
        snap = reload_canary["canary"].collect_snapshot()
        assert snap.zombie_counts == {}
        assert any("docker" in s for s in snap.sources_unavailable)
        from canary.invariants import r01_no_zombie_claude as r01
        assert r01.check(snap) == []


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

    Routes the two `db.*` calls canary_service makes through the real
    `CanaryOperations` (already wired to the temp SQLite via
    `canary_db`). The Slack alert path is observed via the
    `slack_capture` fixture below — this fixture leaves it alone.
    """
    db_canary = reload_canary["db_canary"]
    canary_ops = db_canary.CanaryOperations()

    class _FakeDB:
        def get_latest_canary_violation_per_invariant(self):
            return canary_ops.get_latest_per_invariant()

        def insert_canary_violation(self, **kwargs):
            return canary_ops.insert_violation(**kwargs)

    fake_database = types.ModuleType("database")
    fake_database.db = _FakeDB()
    monkeypatch.setitem(sys.modules, "database", fake_database)

    # Drop any cached canary_service so it picks up the stubs above.
    sys.modules.pop("services.canary_service", None)

    from services.canary_service import CanaryService

    return {
        "service": CanaryService(),
        "canary_ops": canary_ops,
    }


def _run(coro):
    """Run a coroutine to completion in a fresh event loop."""
    import asyncio as _asyncio
    return _asyncio.run(coro)


class TestCanaryService:
    """End-to-end tests for `CanaryService.run_cycle()`."""

    def test_first_cycle_violation_classifies_as_transition(
        self, canary_db, canary_service
    ):
        """First cycle that sees a violation classifies it as a green→red flip."""
        _add_agent(canary_db, "real")
        _add_orphan_sharing(canary_db, "ghost-1")  # triggers L-03

        svc = canary_service["service"]
        result = _run(svc.run_cycle())

        assert result.transition_invariant_ids == ["L-03"]
        assert svc.cumulative_transitions == 1

    def test_continuing_red_does_not_re_classify(self, canary_db, canary_service):
        """Same orphan, three cycles → 3 violations persisted, 1 transition.

        Regression for e7c11b2e: transition detection was firing on every
        continuing-red cycle. The fix uses a Redis previous-cycle cursor
        so a continuously-red invariant is classified once, not every cycle.
        """
        _add_agent(canary_db, "real")
        _add_orphan_sharing(canary_db, "ghost-1")

        svc = canary_service["service"]
        _run(svc.run_cycle())
        _run(svc.run_cycle())
        _run(svc.run_cycle())

        # All three cycles still persist the violation — the forensic
        # record is intact even when the transition counter stays flat.
        ops = canary_service["canary_ops"]
        assert ops.count_violations(invariant_id="L-03") == 3
        assert svc.cumulative_transitions == 1, (
            "continuing-red must not re-classify on every cycle"
        )

    def test_red_green_red_classifies_twice(self, canary_db, canary_service):
        """red → green → red registers two transitions.

        A clean cycle in the middle "re-arms" the invariant; the next
        violation is a fresh transition, not a continuation.
        """
        _add_agent(canary_db, "real")
        _add_orphan_sharing(canary_db, "ghost-1")

        svc = canary_service["service"]

        # Cycle 1: red.
        _run(svc.run_cycle())
        assert svc.cumulative_transitions == 1

        # Cycle 2: clean it up → green.
        c = _conn(canary_db)
        c.execute("DELETE FROM agent_sharing WHERE agent_name='ghost-1'")
        c.commit()
        c.close()
        _run(svc.run_cycle())
        assert svc.cumulative_transitions == 1, "green cycle must not classify"

        # Cycle 3: re-introduce → red again.
        _add_orphan_sharing(canary_db, "ghost-1")
        _run(svc.run_cycle())

        assert svc.cumulative_transitions == 2, (
            "red→green→red must register a fresh transition on the second red"
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

        terminal_seen = fake_redis.zrange("canary:e02:terminal_seen", 0, -1)
        assert "e-real-success" in terminal_seen, (
            "'success' must be in TERMINAL_EXECUTION_STATUSES"
        )
        # Parallel hash must carry the row's real terminal status so a
        # later reversal renders "success → running", not the
        # placeholder "terminal → running" that an earlier Phase 1 cut
        # was emitting into Slack alerts.
        assert (
            fake_redis.hget("canary:e02:terminal_status", "e-real-success")
            == "success"
        )


# ---------------------------------------------------------------------------
# Slack alert sink (CANARY-001 Phase 2)
# ---------------------------------------------------------------------------
#
# These tests exercise the env-gated Slack webhook emit path. The pure
# message-building helpers (CanaryAlerts._build_slack_payload,
# CanaryAlerts._format_last_red) are tested without any fixtures — they're
# static/classmethods. The `CanaryAlerts.emit_transition` integration path
# piggybacks on the existing `canary_service` fixture and stubs the
# slack_service module so we can observe the outbound call without
# touching httpx.


class TestCanarySlackPayload:
    """Pure rendering tests for the Slack payload builder.

    Takes the `canary_service` fixture for its side-effect — importing
    `services.canary_service` reaches `from database import db` at module
    top, which triggers production DB init unless `database` is stubbed.
    The fixture already does that stubbing; we ignore its return value.
    """

    def test_format_last_red_first_red_when_none(self, canary_service):
        from services.canary_alerts import CanaryAlerts
        assert (
            CanaryAlerts._format_last_red(None, "2026-05-04T12:00:00Z")
            == "first red for this invariant"
        )

    def test_format_last_red_seconds(self, canary_service):
        from services.canary_alerts import CanaryAlerts
        out = CanaryAlerts._format_last_red(
            "2026-05-04T11:59:30Z", "2026-05-04T12:00:00Z"
        )
        assert out == "last red 30s ago"

    def test_format_last_red_minutes(self, canary_service):
        from services.canary_alerts import CanaryAlerts
        out = CanaryAlerts._format_last_red(
            "2026-05-04T11:55:00Z", "2026-05-04T12:00:00Z"
        )
        assert out == "last red 5m ago"

    def test_format_last_red_hours(self, canary_service):
        from services.canary_alerts import CanaryAlerts
        out = CanaryAlerts._format_last_red(
            "2026-05-04T10:00:00Z", "2026-05-04T12:30:00Z"
        )
        assert out == "last red 2h ago"

    def test_format_last_red_falls_back_on_garbage(self, canary_service):
        from services.canary_alerts import CanaryAlerts
        out = CanaryAlerts._format_last_red("not-a-timestamp", "2026-05-04T12:00:00Z")
        assert out == "first red for this invariant"

    def test_build_payload_severity_emoji(self, canary_service):
        from services.canary_alerts import CanaryAlerts
        from canary.snapshot import ViolationReport

        v = ViolationReport(
            invariant_id="S-01",
            tier="A",
            severity="critical",
            observed_state={"agent_name": "alpha"},
        )
        text, blocks = CanaryAlerts._build_slack_payload(
            "S-01", [v], "2026-05-04T12:00:00Z", None, "critical", [42],
        )
        assert text.startswith("🚨")
        assert "S-01" in text
        # Header block uses the same emoji + friendly name.
        header = blocks[0]
        assert header["type"] == "header"
        assert "🚨" in header["text"]["text"]
        assert "Slot–row bijection" in header["text"]["text"]

    def test_build_payload_includes_last_red_badge(self, canary_service):
        from services.canary_alerts import CanaryAlerts
        from canary.snapshot import ViolationReport

        v = ViolationReport(
            invariant_id="L-03",
            tier="A",
            severity="major",
            observed_state={"ghost_agent_name": "ghost-1"},
        )
        _, blocks = CanaryAlerts._build_slack_payload(
            "L-03",
            [v],
            "2026-05-04T12:00:00Z",
            "2026-05-04T11:55:00Z",
            "major",
            [21],
        )
        # Context is the last block; assert by type, not index, so
        # added/removed sections don't break this test.
        ctx = next(b for b in blocks if b["type"] == "context")
        assert "last red 5m ago" in ctx["elements"][0]["text"]
        assert "violation #21" in ctx["elements"][0]["text"]

    def test_build_payload_l03_forensic_block(self, canary_service):
        from services.canary_alerts import CanaryAlerts
        from canary.snapshot import ViolationReport

        v = ViolationReport(
            invariant_id="L-03",
            tier="A",
            severity="major",
            observed_state={
                "ghost_agent_name": "ghost-1",
                "tables_hit": ["agent_sharing", "agent_schedules"],
                "sample_refs": [
                    {"table": "agent_sharing", "column": "agent_name", "row_id": "5"},
                    {"table": "agent_schedules", "column": "agent_name", "row_id": "9"},
                ],
            },
        )
        _, blocks = CanaryAlerts._build_slack_payload(
            "L-03", [v], "2026-05-04T12:00:00Z", None, "major", [21],
        )
        sections = [b for b in blocks if b["type"] == "section"]
        forensic_text = " ".join(s["text"]["text"] for s in sections)
        assert "agent_sharing" in forensic_text
        assert "agent_schedules" in forensic_text
        assert "row `5`" in forensic_text
        assert "row `9`" in forensic_text

    def test_build_payload_includes_runbook_hint(self, canary_service):
        from services.canary_alerts import CanaryAlerts
        from canary.snapshot import ViolationReport

        v = ViolationReport(
            invariant_id="L-03",
            tier="A",
            severity="major",
            observed_state={"ghost_agent_name": "ghost-1"},
        )
        _, blocks = CanaryAlerts._build_slack_payload(
            "L-03", [v], "2026-05-04T12:00:00Z", None, "major", [21],
        )
        all_text = " ".join(
            b["text"]["text"] for b in blocks if b.get("text")
        )
        assert "deleted" in all_text  # runbook hint mentions delete handler

    def test_format_row_refs_variants(self, canary_service):
        from services.canary_alerts import CanaryAlerts
        assert CanaryAlerts._format_row_refs([]) is None
        assert CanaryAlerts._format_row_refs([None]) is None
        assert CanaryAlerts._format_row_refs([21]) == "violation #21"
        assert (
            CanaryAlerts._format_row_refs([21, 22, 23])
            == "violations #21, #22, #23"
        )
        assert (
            CanaryAlerts._format_row_refs([21, 22, 23, 24, 25])
            == "violations #21–#25 (5 total)"
        )
        # Drops Nones (insert failures) before counting.
        assert CanaryAlerts._format_row_refs([21, None, 23]) == "violations #21, #23"


@pytest.fixture
def slack_capture(monkeypatch):
    """Replace services.slack_service.slack_service with a recorder.

    The lazy `from services.slack_service import slack_service` inside
    `CanaryAlerts.emit_transition` resolves through `sys.modules`, so
    seeding the module entry up-front captures every call without a
    live httpx client.
    """
    calls: List[Dict[str, Any]] = []
    return_value: Dict[str, Any] = {"value": (True, None)}

    class _Recorder:
        async def post_webhook(self, webhook_url, text, blocks=None, timeout_seconds=5.0):
            calls.append({
                "url": webhook_url,
                "text": text,
                "blocks": blocks,
                "timeout": timeout_seconds,
            })
            return return_value["value"]

    fake = types.ModuleType("services.slack_service")
    fake.slack_service = _Recorder()
    monkeypatch.setitem(sys.modules, "services.slack_service", fake)

    return {"calls": calls, "return_value": return_value}


class TestCanarySlackEmit:
    """Integration tests for `CanaryAlerts.emit_transition` against a recorded sink."""

    def test_no_webhook_url_skips_silently(
        self, canary_db, canary_service, slack_capture, monkeypatch
    ):
        """No env var = no POST. Cycle still runs, violation still persists."""
        monkeypatch.delenv("CANARY_SLACK_WEBHOOK_URL", raising=False)
        _add_agent(canary_db, "real")
        _add_orphan_sharing(canary_db, "ghost-1")

        svc = canary_service["service"]
        result = _run(svc.run_cycle())

        assert result.transition_invariant_ids == ["L-03"]
        assert slack_capture["calls"] == [], "no webhook URL must not POST"
        # Violation still persisted.
        ops = canary_service["canary_ops"]
        assert ops.count_violations(invariant_id="L-03") == 1

    def test_webhook_url_set_fires_one_post_per_transition(
        self, canary_db, canary_service, slack_capture, monkeypatch
    ):
        """With env var set, exactly one webhook POST per transition."""
        monkeypatch.setenv(
            "CANARY_SLACK_WEBHOOK_URL",
            "https://hooks.slack.com/services/TEST/TEST/TEST",
        )
        _add_agent(canary_db, "real")
        _add_orphan_sharing(canary_db, "ghost-1")

        svc = canary_service["service"]
        _run(svc.run_cycle())

        assert len(slack_capture["calls"]) == 1
        call = slack_capture["calls"][0]
        assert call["url"] == "https://hooks.slack.com/services/TEST/TEST/TEST"
        assert "L-03" in call["text"]
        # Block layout has grown beyond the original 3 — assert by type
        # rather than count so future copy edits don't trip this test.
        block_types = [b["type"] for b in call["blocks"]]
        assert block_types[0] == "header"
        assert block_types[-1] == "context"
        assert "section" in block_types

    def test_continuing_red_does_not_re_post(
        self, canary_db, canary_service, slack_capture, monkeypatch
    ):
        """Three cycles with the same red invariant = one webhook POST.

        Mirrors `test_continuing_red_does_not_re_classify` — green→red
        gating runs upstream of the sink, so the sink also fires once.
        """
        monkeypatch.setenv(
            "CANARY_SLACK_WEBHOOK_URL",
            "https://hooks.slack.com/services/TEST/TEST/TEST",
        )
        _add_agent(canary_db, "real")
        _add_orphan_sharing(canary_db, "ghost-1")

        svc = canary_service["service"]
        _run(svc.run_cycle())
        _run(svc.run_cycle())
        _run(svc.run_cycle())

        assert len(slack_capture["calls"]) == 1, (
            "continuing-red must POST once, not every cycle"
        )

    def test_webhook_failure_swallowed_cycle_continues(
        self, canary_db, canary_service, slack_capture, monkeypatch
    ):
        """A failing webhook must not break cycle accounting.

        The row is already persisted before `CanaryAlerts.emit_transition` runs;
        a hung Slack endpoint can't roll that back. We assert the
        transition is still counted and the violation is still in the
        DB even when the recorder returns a failure tuple.
        """
        monkeypatch.setenv(
            "CANARY_SLACK_WEBHOOK_URL",
            "https://hooks.slack.com/services/TEST/TEST/TEST",
        )
        slack_capture["return_value"]["value"] = (False, "invalid_token")
        _add_agent(canary_db, "real")
        _add_orphan_sharing(canary_db, "ghost-1")

        svc = canary_service["service"]
        result = _run(svc.run_cycle())

        assert result.transition_invariant_ids == ["L-03"]
        assert svc.cumulative_transitions == 1
        ops = canary_service["canary_ops"]
        assert ops.count_violations(invariant_id="L-03") == 1
        # Recorder still saw the call — failure happened on Slack's side.
        assert len(slack_capture["calls"]) == 1

    def test_previous_violation_at_threaded_into_payload(
        self, canary_db, canary_service, slack_capture, monkeypatch
    ):
        """red→green→red: second-red POST carries the prior snapshot_time
        so the alert can render "last red Xm ago".
        """
        monkeypatch.setenv(
            "CANARY_SLACK_WEBHOOK_URL",
            "https://hooks.slack.com/services/TEST/TEST/TEST",
        )
        _add_agent(canary_db, "real")
        _add_orphan_sharing(canary_db, "ghost-1")

        svc = canary_service["service"]

        # Cycle 1: first-ever transition → "first red" badge.
        _run(svc.run_cycle())
        first_ctx = next(
            b for b in slack_capture["calls"][0]["blocks"] if b["type"] == "context"
        )["elements"][0]["text"]
        assert "first red" in first_ctx

        # Cycle 2: clean → green.
        c = _conn(canary_db)
        c.execute("DELETE FROM agent_sharing WHERE agent_name='ghost-1'")
        c.commit()
        c.close()
        _run(svc.run_cycle())
        # Cycle 3: re-introduce → second transition.
        _add_orphan_sharing(canary_db, "ghost-1")
        _run(svc.run_cycle())

        assert len(slack_capture["calls"]) == 2
        second_ctx = next(
            b for b in slack_capture["calls"][1]["blocks"] if b["type"] == "context"
        )["elements"][0]["text"]
        assert "last red" in second_ctx, (
            "second transition must carry the prior snapshot_time"
        )
