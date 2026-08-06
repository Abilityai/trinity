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


# Modules this file stubs into sys.modules at *import time* (the three blocks
# above run at collection, before any fixture, so monkeypatch can't reach
# them). Without restoration they LEAK into the pytest session — the exact
# mechanism behind the #1446 B-01 flake (a foreign `database` MagicMock leaked
# by a sibling file). The autouse `_restore_sys_modules` fixture below snapshots
# and restores them around every test so a canary run can't pollute a later
# file that re-imports the real modules. This pair also lints the whole file
# clean under tests/lint_sys_modules.py (the sanctioned escape hatch, precedent:
# tests/unit/test_telegram_webhook_backfill.py) — so the deliberate reimport
# `del`s in `reload_canary`/`reload_canary_split` are covered too.
_STUBBED_MODULE_NAMES = [
    "utils.helpers",
    "croniter",
    "models",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot the import-time-stubbed modules before each test, restore after.

    Keeps this file's stubs from leaking into other test files in the same
    pytest session (#762 / #1446 discipline).
    """
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


# ---------------------------------------------------------------------------
# Tiny in-memory Redis substitute — covers only the surface canary uses.
# ---------------------------------------------------------------------------


class _FakePipeline:
    """Records buffered commands and replays them against the parent FakeRedis.

    Deliberately NOT a no-op passthrough: the point of the pipeline in
    `_collect_redis_slot_state` is that both reads land in one round-trip, and
    a double that executed eagerly would let a test pass against code that
    issued them separately.
    """

    def __init__(self, parent: "FakeRedis"):
        self._parent = parent
        self._queued: List[tuple] = []

    def ttl(self, key: str) -> "_FakePipeline":
        self._queued.append(("ttl", (key,)))
        return self

    def hget(self, key: str, field: str) -> "_FakePipeline":
        self._queued.append(("hget", (key, field)))
        return self

    def execute(self) -> List:
        results = [getattr(self._parent, op)(*args) for op, args in self._queued]
        self._queued.clear()
        return results


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

    def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self._hashes.get(key, {}))

    def hkeys(self, key: str) -> List[str]:
        return list(self._hashes.get(key, {}).keys())

    def hlen(self, key: str) -> int:
        return len(self._hashes.get(key, {}))

    # PIPELINE --------------------------------------------------------------
    # `_collect_redis_slot_state` reads each slot's TTL and its stored
    # `timeout_seconds` in one round-trip (ent#336). Buffer the calls and
    # replay them on execute(), mirroring redis-py's chaining API.

    def pipeline(self):
        return _FakePipeline(self)

    def expire(self, key: str, seconds: int) -> bool:
        exists = key in self._hashes or key in self._zsets or key in self._strings
        if exists:
            self._ttls[key] = seconds
        return exists

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


# Schema for the tables the canary collector touches. One source of truth so
# `canary_db` (single file) and `canary_db_split` (raw ≠ engine, #1450) can't drift.
_CANARY_SCHEMA_SQL = """
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
            duration_ms INTEGER,          -- #1077 E-03: terminal-row timing
            queued_at TEXT,               -- #1077 E-04: backlog enqueue time
            backlog_metadata TEXT,        -- #1077 E-04/G-04: JSON drain-replay identity
            message TEXT NOT NULL DEFAULT '',
            triggered_by TEXT NOT NULL DEFAULT 'test',
            lease_expires_at TEXT  -- #1081 Phase 3: non-NULL = pull-claimed row
        );
        -- Mirrors the production agent_schedules columns the canary reads
        -- (E-06 reads next_run_at/enabled/deleted_at; L-03 reads agent_name).
        -- NOTE: a single definition — a stray second `CREATE TABLE
        -- agent_schedules` here (from a #1472 merge) made executescript raise
        -- `table already exists`, reddening the whole file (#1077 baseline fix).
        CREATE TABLE agent_schedules (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            cron_expression TEXT NOT NULL DEFAULT '0 4 * * *',
            message TEXT NOT NULL DEFAULT '',
            enabled INTEGER DEFAULT 1,
            next_run_at TEXT,
            owner_id INTEGER NOT NULL DEFAULT 0,
            deleted_at TEXT
        );
        CREATE TABLE agent_sharing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            shared_with_email TEXT NOT NULL,
            shared_by_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
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
        -- #1540: production agent_tags is composite-PK (agent_name, tag) with
        -- NO `id` column (db/tables.py). The canary derives the orphan-scan PK
        -- from the STATIC Table, so a composite PK yields a synthetic
        -- `agent_tags-row` row_id. Matching the real DDL here exercises that
        -- composite branch on the SQLite rail (guards the KeyError('id') a naive
        -- reflected-PK port would have shipped).
        CREATE TABLE agent_tags (
            agent_name TEXT NOT NULL,
            tag TEXT NOT NULL,
            created_at TEXT,
            PRIMARY KEY (agent_name, tag)
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


def _create_canary_db_file():
    """Create a temp SQLite file carrying `_CANARY_SCHEMA_SQL`; return its path."""
    db_file = tempfile.NamedTemporaryFile(suffix="_canary_test.db", delete=False)
    db_file.close()
    conn = sqlite3.connect(db_file.name)
    conn.executescript(_CANARY_SCHEMA_SQL)
    conn.commit()
    conn.close()
    return db_file.name


def _make_db_connection_module(db_path):
    """A fake `db.connection` module whose `get_db_connection()` opens db_path."""

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

    mod = types.ModuleType("db.connection")
    mod.get_db_connection = lambda: _ConnCtx()
    return mod


@pytest.fixture
def canary_db(monkeypatch):
    """Temp SQLite with the tables canary touches; patch db.connection.

    The raw-sqlite `db.connection` stub and the `get_engine()`/DATABASE_URL seam
    point at the SAME temp file, so B-01's two sides read identical rows.
    `canary_db_split` decouples them to model a Postgres-style diverged backend.
    """
    db_path = _create_canary_db_file()

    monkeypatch.setitem(
        sys.modules, "db.connection", _make_db_connection_module(db_path)
    )

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
def canary_db_split(monkeypatch):
    """#1450: decouple the raw-sqlite reader from the engine backend.

    Models a Postgres-style diverged backend on one machine WITHOUT a live PG:

    - `db.connection` (raw sqlite3 — known-agents + running/queued id-lists)
      → the RAW file.
    - `get_engine()`/DATABASE_URL (`db.get_queued_count` Side A +
      `_collect_queued_ids_via_engine` Side B) → the ENGINE file.

    Both carry the full schema. Yields `(raw_path, engine_path)`. On the
    single-file `canary_db` these are one file and the split is invisible (as on
    SQLite prod); here they are two databases — exactly the split B-01 must now
    tolerate by sourcing both its sides from the engine.
    """
    raw_path = _create_canary_db_file()
    engine_path = _create_canary_db_file()

    monkeypatch.setitem(
        sys.modules, "db.connection", _make_db_connection_module(raw_path)
    )
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{engine_path}")
    import db.engine as engine_mod
    engine_mod.dispose_engines()

    yield raw_path, engine_path

    engine_mod.dispose_engines()
    os.unlink(raw_path)
    os.unlink(engine_path)


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


class _CanaryTempDB:
    """Minimal `database.db` stand-in for B-01.

    `get_queued_count` counts `schedule_executions` queued rows over the temp
    `DATABASE_URL` using the same `get_engine()` factory the real
    `ScheduleOperations.get_queued_count` uses — so B-01 Side A (the accessor)
    and Side B (`_collect_queued_ids_via_engine`) read the same rows in the same
    backend. Kept SQL-string-based (not the SQLAlchemy Core select the real
    accessor uses) so it stays an independent code path — B-01 stays a genuine
    coherence check, not a tautology.
    """

    def get_queued_count(self, agent_name: str) -> int:
        from sqlalchemy import text as _text
        from db.engine import get_engine

        with get_engine().connect() as conn:
            row = conn.execute(
                _text(
                    "SELECT COUNT(*) AS c FROM schedule_executions "
                    "WHERE agent_name = :n AND status = 'queued'"
                ),
                {"n": agent_name},
            ).first()
        return int(row[0]) if row else 0

    def get_setting_value(self, key: str, default=None):
        # No settings table in the B-01 temp DB; mirror the real facade's
        # empty-store return so callers that leak onto this stub via the
        # module-level `from database import db` reference (settings_service.
        # get_setting <- task_execution_service.get_platform_default_model)
        # get a clean fallback instead of an AttributeError.
        return default


def _reload_canary_with_temp_db(fake_redis, monkeypatch):
    """Evict + reimport the canary modules and install the controlled `database`
    stub. Shared by `reload_canary` (single file) and `reload_canary_split`
    (raw ≠ engine). The `del sys.modules[...]` reimport is lint-clean under the
    file's `_STUBBED_MODULE_NAMES`/`_restore_sys_modules` escape hatch.

    Why a controlled `database` stub at all — the snapshot's
    `_collect_queued_count_via_service` does `from database import db;
    int(db.get_queued_count(name))` at *call* time, resolving whatever
    `sys.modules["database"]` holds. Under full-suite load another module leaks a
    bare `sys.modules.setdefault("database", MagicMock())` (e.g.
    tests/test_watchdog_unit.py) that never tears down;
    `int(MagicMock().get_queued_count("a1")) == 1`, so B-01 saw `service_count=1`
    against a 0-queued temp DB and false-fired (#1446). `monkeypatch.setitem`
    overrides that leak for the test's duration and delegates to the temp DB via
    the exact production count path — genuine, not a tautology. We deliberately
    do NOT evict-and-reimport the real `database` module (its `__init__` runs
    `init_database()`/migrations — heavy, module-identity churn).
    """
    for mod in list(sys.modules):
        if mod.startswith("canary") or mod == "db.canary":
            del sys.modules[mod]

    database_stub = types.ModuleType("database")
    database_stub.db = _CanaryTempDB()
    monkeypatch.setitem(sys.modules, "database", database_stub)

    import canary as canary_pkg  # noqa: F401
    import db.canary as db_canary

    return {
        "canary": canary_pkg,
        "db_canary": db_canary,
        "redis": fake_redis,
        "database_stub": database_stub,
    }


@pytest.fixture
def reload_canary(canary_db, fake_redis, fake_docker, monkeypatch):
    """Force reimport of canary modules bound to the patched modules, over the
    single-file `canary_db` (raw and engine reads hit the same temp DB)."""
    return _reload_canary_with_temp_db(fake_redis, monkeypatch)


@pytest.fixture
def reload_canary_split(canary_db_split, fake_redis, fake_docker, monkeypatch):
    """Like `reload_canary` but over the split (raw ≠ engine) backend (#1450) —
    `db.connection` reads the RAW file, `get_engine()`/DATABASE_URL the ENGINE
    file. Proves B-01 sources both its sides from the engine."""
    return _reload_canary_with_temp_db(fake_redis, monkeypatch)


@pytest.fixture
def leaked_magicmock_database(monkeypatch):
    """Reproduce the #1446 flake: a foreign `database` MagicMock leaked into
    `sys.modules` by another test module.

    Faithful to the real leak — `tests/test_watchdog_unit.py` (and siblings)
    do a module-level `sys.modules.setdefault("database", MagicMock())` that is
    never restored. `int(MagicMock().get_queued_count("a1")) == 1`, so under
    full-suite ordering the canary's `_collect_queued_count_via_service` read
    `service_count=1` against a temp DB with 0 queued rows and B-01 false-fired.

    Declare this fixture *ahead of* `reload_canary` in a test's signature so the
    leak is installed BEFORE `reload_canary` (mirroring the real world: the leak
    precedes the canary test). Both use `monkeypatch.setitem` on the shared
    monkeypatch (last-write-wins); `reload_canary` therefore overrides this leak,
    which is exactly the property under test. Auto-restored LIFO at teardown.
    """
    from unittest.mock import MagicMock

    leaked = types.ModuleType("database")
    leaked.db = MagicMock()
    monkeypatch.setitem(sys.modules, "database", leaked)
    return leaked


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


class _Sequence:
    """Callable returning successive values on each call (ignoring the arg).

    Used by the #1450 confirm-re-read tests to script what
    `_collect_queued_ids_via_engine` / `_collect_queued_count_via_service`
    return across the two reads of one B-01 cycle. `.calls` counts invocations
    so a test can assert the confirm-re-read actually happened.
    """

    def __init__(self, values):
        self._values = list(values)
        self.calls = 0

    def __call__(self, agent_name):
        value = self._values[min(self.calls, len(self._values) - 1)]
        self.calls += 1
        return value


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


def _add_execution(
    path,
    eid,
    agent_name,
    status,
    started_at=None,
    completed_at=None,
    duration_ms=None,
    queued_at=None,
    backlog_metadata=None,
    lease_expires_at=None,
):
    c = _conn(path)
    c.execute(
        "INSERT INTO schedule_executions "
        "(id, agent_name, status, started_at, completed_at, duration_ms, "
        "queued_at, backlog_metadata, lease_expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            eid,
            agent_name,
            status,
            started_at or "2026-04-30T00:00:00Z",
            completed_at,
            duration_ms,
            queued_at,
            backlog_metadata,
            lease_expires_at,
        ),
    )
    c.commit()
    c.close()


def _add_schedule(path, sid, agent_name, next_run_at, enabled=1, deleted_at=None):
    c = _conn(path)
    c.execute(
        "INSERT INTO agent_schedules (id, agent_name, enabled, next_run_at, deleted_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (sid, agent_name, enabled, next_run_at, deleted_at),
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

    def test_queued_meta_collected_for_queued_rows_only(self, canary_db, reload_canary):
        # #1077 E-04/G-04: queued rows expose queued_at + backlog_metadata via
        # `queued_meta`, keyed by execution_id and scoped STRICTLY to queued
        # rows — a terminal row's metadata must NOT appear (so #1449's future
        # terminal-row NULL-out can't false-fire E-04).
        _add_agent(canary_db, "a1")
        _add_execution(
            canary_db, "e-q", "a1", "queued",
            queued_at="2026-05-18T10:00:00Z",
            backlog_metadata='{"trigger": "schedule"}',
        )
        # A terminal row that also carries backlog_metadata — must be excluded.
        _add_execution(
            canary_db, "e-done", "a1", "success",
            completed_at="2026-05-18T10:05:00Z",
            queued_at="2026-05-18T09:00:00Z",
            backlog_metadata='{"trigger": "schedule"}',
        )
        snap = reload_canary["canary"].collect_snapshot()
        agent = snap.agents[0]
        assert set(agent.queued_meta.keys()) == {"e-q"}
        assert agent.queued_meta["e-q"]["queued_at"] == "2026-05-18T10:00:00Z"
        assert agent.queued_meta["e-q"]["backlog_metadata"] == '{"trigger": "schedule"}'


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

    def test_leased_pull_rows_excluded_from_sql_side(self, canary_db, reload_canary):
        """#1081 Phase 3: a pilot agent with an EMPTY ZSET and N running rows
        that are pull-CLAIMED (lease_expires_at IS NOT NULL) must NOT fire S-01
        — leased rows legitimately never enter the slot ZSET."""
        _add_agent(canary_db, "pilot")
        # Two leased pull rows, running but never slotted; ZSET stays empty.
        _add_execution(
            canary_db, "e-lease-1", "pilot", "running",
            lease_expires_at="2026-12-31T00:00:00Z",
        )
        _add_execution(
            canary_db, "e-lease-2", "pilot", "running",
            lease_expires_at="2026-12-31T00:00:00Z",
        )
        # No Redis ZADD for either — the whole point of the pull path.

        snap = reload_canary["canary"].collect_snapshot()
        # Precondition: the SQL rows ARE running (so this isn't a vacuous pass).
        agent = next(a for a in snap.agents if a.name == "pilot")
        assert agent.running_exec_ids == {"e-lease-1", "e-lease-2"}
        assert agent.slot_ids == set()

        from canary.invariants import s01_slot_row_bijection as s01

        assert s01.check(snap) == [], (
            "leased pull rows must not be counted on the SQL side of S-01"
        )

    def test_non_leased_orphan_fires_even_alongside_leased(
        self, canary_db, reload_canary
    ):
        """No regression: a genuine slot–row mismatch on a NON-leased (push)
        row still fires S-01, and the exclusion is selective — a co-resident
        leased row is ignored while the real orphan is flagged."""
        _add_agent(canary_db, "pilot")
        # Leased pull row: running, no slot, must be ignored.
        _add_execution(
            canary_db, "e-lease", "pilot", "running",
            lease_expires_at="2026-12-31T00:00:00Z",
        )
        # Non-leased (push) row: running, no slot → a real S-01 violation.
        _add_execution(canary_db, "e-push-orphan", "pilot", "running")
        # ZSET empty for both.

        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s01_slot_row_bijection as s01

        violations = s01.check(snap)
        assert len(violations) == 1
        obs = violations[0].observed_state
        # Only the push row is flagged; the leased row is excluded entirely.
        assert obs["in_sql_only"] == ["e-push-orphan"]
        assert obs["sql_running_count"] == 1  # leased row not counted


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
            "E-01", "E-02", "E-03", "E-04", "E-05", "E-06",
            "G-03", "G-04",
            "L-03",
            "B-01", "B-02",
            "R-01",
        }
        assert results["S-01"] == []
        assert results["S-02"] == []
        assert results["S-03"] == []
        assert results["E-01"] == []
        assert results["E-02"] == []
        # E-03/G-03/E-04/G-04 (#1077) hold on a clean platform — no terminal or
        # queued rows seeded.
        assert results["E-03"] == []
        assert results["E-04"] == []
        assert results["E-05"] == []
        assert results["E-06"] == []
        assert results["G-03"] == []
        assert results["G-04"] == []
        assert len(results["L-03"]) == 1
        # B-01 now reads the SAME temp DB on both sides via reload_canary's
        # controlled `database` stub (get_queued_count over the temp
        # DATABASE_URL). With zero queued rows the service count == the
        # snapshot id-list count == 0, so the check is genuinely green — not
        # skipped, and immune to a leaked foreign `database` stub (#1446).
        assert results["B-01"] == []
        # E-06 (stale next_run_at) is green with no schedules present.
        assert results["E-06"] == []
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
    def _snap(*, started_at, session_id, lease_expires_at=None):
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
                    running_lease_expires_at={"e1": lease_expires_at},
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

    # -- #1766: pull-claimed rows are excluded ------------------------------

    def test_leased_row_is_excluded(self):
        """A pull-CLAIMED row is `running` with a NULL claude_session_id BY
        DESIGN — `mark_no_session_executions_failed` already skips leased rows,
        so E-05 must too or it fires on every pull turn for a whole soak."""
        snap = self._snap(
            started_at="2026-05-18T11:00:00Z",   # 1h old
            session_id=None,                     # no session — would fire
            lease_expires_at="2026-05-18T12:30:00Z",
        )
        from canary.invariants import e05_dispatched_rows_have_session as e05
        assert e05.check(snap) == []

    def test_non_leased_control_still_fires(self):
        """The exclusion must be keyed on the lease, not blanket-silence E-05:
        an identical row with a NULL lease is still the #106 bug class."""
        snap = self._snap(
            started_at="2026-05-18T11:00:00Z",
            session_id=None,
            lease_expires_at=None,
        )
        from canary.invariants import e05_dispatched_rows_have_session as e05
        assert len(e05.check(snap)) == 1


# ---------------------------------------------------------------------------
# B-01 — queue-status coherence
# ---------------------------------------------------------------------------


class TestInvariantB01:
    @staticmethod
    def _snap(*, queued_ids, service_count, engine_ids="__same__"):
        """Build a one-agent snapshot for B-01.

        `queued_ids` populates the B-02/E-02 raw set (`queued_exec_ids`) for
        realism; B-01's Side B is `queued_ids_via_engine` (#1450), which
        defaults to the same set unless a test overrides `engine_ids` to
        decouple the two backends (e.g. to assert the engine-None skip).
        """
        from canary.snapshot import Snapshot, AgentSnapshot
        engine = set(queued_ids) if engine_ids == "__same__" else engine_ids
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
                    queued_ids_via_engine=(
                        None if engine is None else set(engine)
                    ),
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
        # B-01 now reports the engine-side ids (Side B), not the raw set.
        assert v.observed_state["snapshot_queued_ids"] == ["q1", "q2", "q3"]

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

    def test_skips_when_engine_ids_none(self):
        """#1450: engine-side read failed (`queued_ids_via_engine is None`) →
        B-01 skips rather than comparing the accessor count to a raw-sqlite
        set on a diverged backend."""
        snap = self._snap(queued_ids={"q1", "q2"}, service_count=2, engine_ids=None)
        from canary.invariants import b01_queue_status_coherence as b01
        assert b01.check(snap) == []


# ---------------------------------------------------------------------------
# B-01 — end-to-end coherence over the real snapshot collector (#1446)
# ---------------------------------------------------------------------------
#
# The synthetic tests above hand-build a Snapshot. These drive the full
# `collect_snapshot()` → `run_invariants`/`b01.check` path against the temp
# SQLite, so the service side (`db.get_queued_count`) and the id-list side
# (`_collect_executions`) both read the same DB — the first genuine e2e
# exercise of B-01, and the regression net for the #1446 `sys.modules`
# `database` leak that let it false-fire under full-suite load.


class TestInvariantB01EndToEnd:
    def test_coherent_with_queued_rows(self, canary_db, reload_canary):
        """N real queued rows → both sides count N → B-01 green.

        Proves the controlled stub delegates to the temp DB (not a hand-fed
        constant): insert queued rows and B-01 stays green because the service
        count tracks them.
        """
        _add_agent(canary_db, "a1")
        for i in range(3):
            _add_execution(canary_db, f"q{i}", "a1", "queued")

        snap = reload_canary["canary"].collect_snapshot()
        agent = next(a for a in snap.agents if a.name == "a1")
        assert agent.queued_count_via_service == 3
        assert len(agent.queued_exec_ids) == 3

        results = reload_canary["canary"].run_invariants(snap)
        assert results["B-01"] == []

    def test_immune_to_leaked_database_magicmock(
        self, canary_db, leaked_magicmock_database, reload_canary
    ):
        """#1446 regression — the exact 2026-07-03/04 flake.

        A foreign `database` MagicMock is leaked into `sys.modules` BEFORE
        `reload_canary` runs (via the `leaked_magicmock_database` fixture
        declared ahead of `reload_canary` in the signature). Left unchecked,
        `int(MagicMock().get_queued_count("a1")) == 1` would make B-01 fire
        with `service_count=1` against a 0-queued temp DB. `reload_canary`'s
        controlled stub must override the leak so B-01 stays green.
        """
        # Sanity: the leak really does yield the tell-tale service_count=1.
        assert int(leaked_magicmock_database.db.get_queued_count("a1")) == 1

        _add_agent(canary_db, "a1")  # zero queued rows

        snap = reload_canary["canary"].collect_snapshot()
        agent = next(a for a in snap.agents if a.name == "a1")
        assert agent.queued_count_via_service == 0, (
            "controlled temp-DB stub must override the leaked MagicMock"
        )

        results = reload_canary["canary"].run_invariants(snap)
        assert results["B-01"] == [], (
            "leaked foreign `database` must not trip B-01 after the #1446 fix"
        )

    def test_fires_on_service_count_drift(self, canary_db, reload_canary):
        """Real tripwire coverage: force the accessor to disagree with the
        snapshot id-list → B-01 fires once with the correct observed counts.

        This is the drift B-01 exists to catch — a cache / status-filter
        regression on `db.get_queued_count`. Overriding the controlled stub to
        undercount simulates exactly that.
        """
        _add_agent(canary_db, "a1")
        _add_execution(canary_db, "q1", "a1", "queued")
        _add_execution(canary_db, "q2", "a1", "queued")

        # Simulate a cache/status-filter regression: the accessor undercounts.
        reload_canary["database_stub"].db.get_queued_count = lambda name: 0

        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import b01_queue_status_coherence as b01

        violations = b01.check(snap)
        assert len(violations) == 1
        v = violations[0]
        assert v.invariant_id == "B-01"
        assert v.severity == "critical"
        assert v.observed_state["agent_name"] == "a1"
        assert v.observed_state["service_count"] == 0
        assert v.observed_state["snapshot_count"] == 2

    # -- #1450 regression net -------------------------------------------------

    def test_b01_backend_consistency_on_diverged_backend(
        self, canary_db_split, reload_canary_split
    ):
        """#1450 + #1540: a Postgres-style diverged backend WITHOUT a live PG.

        `canary_db_split` decouples the raw-sqlite file (`db.connection`) from the
        `get_engine()`/DATABASE_URL ENGINE file. Since #1540 the WHOLE collector
        reads the ENGINE file, so the agent + queued rows live there and BOTH
        B-01 sides (accessor Side A, `_collect_queued_ids_via_engine` Side B) —
        and now `queued_exec_ids` from `_collect_executions` too — read the same
        engine backend. B-01 stays green (N == N); the RAW file is ignored.
        """
        raw_path, engine_path = canary_db_split
        # The agent + its queued rows live in the ENGINE file — that is what
        # _collect_known_agents and every SQL collector reads since #1540.
        _add_agent(engine_path, "a1")
        for i in range(3):
            _add_execution(engine_path, f"q{i}", "a1", "queued")
        # Decoy state written ONLY to the RAW file: a ghost agent + a queued row.
        # Post-#1540 these MUST NOT leak into any collector (proof the reads moved
        # off db.connection onto the engine seam).
        _add_agent(raw_path, "raw-only-ghost")
        _add_execution(raw_path, "raw-q", "a1", "queued")

        snap = reload_canary_split["canary"].collect_snapshot()

        # _collect_known_agents reads the ENGINE file → the RAW-only ghost is
        # invisible; the engine agent is present.
        assert snap.known_agents == {"a1"}
        agent = next(a for a in snap.agents if a.name == "a1")

        # Both B-01 sides AND queued_exec_ids read the engine → all see the 3
        # engine rows; the raw "raw-q" row is ignored.
        assert agent.queued_ids_via_engine == {"q0", "q1", "q2"}
        assert agent.queued_exec_ids == {"q0", "q1", "q2"}
        assert agent.queued_count_via_service == 3

        results = reload_canary_split["canary"].run_invariants(snap)
        assert results["B-01"] == [], (
            "B-01 must not false-fire when the raw-sqlite reader and the engine "
            "backend diverge — the whole collector now reads get_engine() "
            "(#1450 / #1540)"
        )

    def test_b01_tolerates_transient_race(
        self, canary_db, reload_canary, monkeypatch
    ):
        """#1450 gap (a): a concurrent enqueue/drain between the two reads is a
        transient mismatch the single confirm-re-read absorbs.

        First read pair (count 3 vs 2 ids) triggers the confirm; the confirm
        pair agrees (3 vs 3) → B-01 green, and both readers were called twice.
        """
        import canary.snapshot as snap_mod

        _add_agent(canary_db, "a1")

        ids_seq = _Sequence([{"q1", "q2"}, {"q1", "q2", "q3"}])
        count_seq = _Sequence([3, 3])
        # setattr on the REIMPORTED module (collect_snapshot resolves these names
        # in its own module globals at call time) — never a raw sys.modules poke.
        monkeypatch.setattr(snap_mod, "_collect_queued_ids_via_engine", ids_seq)
        monkeypatch.setattr(snap_mod, "_collect_queued_count_via_service", count_seq)

        snap = reload_canary["canary"].collect_snapshot()
        results = reload_canary["canary"].run_invariants(snap)

        assert results["B-01"] == []  # transient race absorbed by the confirm
        assert ids_seq.calls == 2  # confirm-re-read happened
        assert count_seq.calls == 2

    def test_b01_confirm_does_not_mask_persistent_drift(
        self, canary_db, reload_canary, monkeypatch
    ):
        """Companion to the race test — the confirm-re-read must NOT hide a real
        regression. A persistent drift (count 0 vs 2 ids on BOTH reads) survives
        the confirm and still fires exactly once (AC #3 intact)."""
        import canary.snapshot as snap_mod

        _add_agent(canary_db, "a1")
        ids_seq = _Sequence([{"q1", "q2"}, {"q1", "q2"}])
        count_seq = _Sequence([0, 0])
        monkeypatch.setattr(snap_mod, "_collect_queued_ids_via_engine", ids_seq)
        monkeypatch.setattr(snap_mod, "_collect_queued_count_via_service", count_seq)

        snap = reload_canary["canary"].collect_snapshot()
        results = reload_canary["canary"].run_invariants(snap)
        assert len(results["B-01"]) == 1
        assert results["B-01"][0].observed_state["service_count"] == 0
        assert results["B-01"][0].observed_state["snapshot_count"] == 2
        # Confirm was attempted (both readers called twice), yet still fired.
        assert ids_seq.calls == 2
        assert count_seq.calls == 2

    def test_b01_skips_on_engine_read_failure(
        self, canary_db, reload_canary, monkeypatch
    ):
        """The blocker's net: an engine-side read failure degrades B-01 to a
        SKIP (never a raw-vs-engine false-fire). `queued_ids_via_engine` is
        `None`, B-01 returns [], and the failure is recorded in
        `sources_unavailable`."""
        import canary.snapshot as snap_mod

        _add_agent(canary_db, "a1")
        _add_execution(canary_db, "q1", "a1", "queued")
        _add_execution(canary_db, "q2", "a1", "queued")

        def _boom(agent_name):
            raise RuntimeError("engine down")

        monkeypatch.setattr(snap_mod, "_collect_queued_ids_via_engine", _boom)

        snap = reload_canary["canary"].collect_snapshot()
        agent = next(a for a in snap.agents if a.name == "a1")
        assert agent.queued_ids_via_engine is None
        # The accessor still returned 2 — but B-01 must NOT compare 2 against the
        # raw set; it skips because the engine set is None.
        assert agent.queued_count_via_service == 2
        assert any(
            s.startswith("engine.queued_ids[a1]") for s in snap.sources_unavailable
        )

        results = reload_canary["canary"].run_invariants(snap)
        assert results["B-01"] == []


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

    @staticmethod
    def _store_timeout(redis, agent: str, eid: str, timeout: int) -> None:
        """Write the `timeout_seconds` field `SlotService.acquire_slot` records.

        ent#336: S-03's floor comes from THIS field, not the agent cap. Every
        real slot has it; a test that omits it is exercising the
        unobservable-timeout skip path, not the floor.
        """
        redis.hset(f"agent:slot:{agent}:{eid}", "timeout_seconds", str(timeout))

    def test_holds_when_ttl_above_floor(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1", timeout=60)
        reload_canary["redis"].zadd("agent:slots:a1", {"e1": self._now_score()})
        self._store_timeout(reload_canary["redis"], "a1", "e1", 60)  # floor 360
        reload_canary["redis"].set_ttl("agent:slot:a1:e1", 500)
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s03_slot_ttl_floor as s03
        assert s03.check(snap) == []

    def test_fires_below_floor(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1", timeout=60)
        # Slot acquired ~now, stored timeout 60 → floor 360, but EXPIRE=100.
        # Initial TTL = 100 + ~0 age = 100 < 360 — the EXPIRE and the HSET
        # disagree, which is what `below_floor` now means (ent#336).
        reload_canary["redis"].zadd("agent:slots:a1", {"e1": self._now_score()})
        self._store_timeout(reload_canary["redis"], "a1", "e1", 60)
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
        assert v[0].observed_state["floor_source"] == "stored"
        assert v[0].observed_state["stored_timeout_seconds"] == 60

    def test_natural_decay_not_below_floor(self, canary_db, reload_canary):
        """#913 regression — a slot created with EXPIRE=floor and observed
        seconds later must not fire just because of natural TTL decay."""
        import time
        _add_agent(canary_db, "a1", timeout=60)  # floor = 360s
        # Slot was created 5s ago with the canonical EXPIRE=floor. Real
        # TTL now reads `360 - 5 = 355` — below the raw floor — but the
        # reconstructed initial TTL is 360, exactly at the floor.
        reload_canary["redis"].zadd("agent:slots:a1", {"e1": time.time() - 5})
        self._store_timeout(reload_canary["redis"], "a1", "e1", 60)
        reload_canary["redis"].set_ttl("agent:slot:a1:e1", 355)
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s03_slot_ttl_floor as s03
        assert s03.check(snap) == []

    def test_fires_when_metadata_missing(self, canary_db, reload_canary):
        """ZSET points at a slot whose metadata HASH already expired (#226).

        The HASH is gone, so `timeout_seconds` is unreadable — this arm must
        still fire, because it is independent of the floor (ent#336).
        """
        _add_agent(canary_db, "a1", timeout=60)
        reload_canary["redis"].zadd("agent:slots:a1", {"e1": self._now_score()})
        # FakeRedis.ttl returns -2 when neither the hash nor the ttl is set.
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s03_slot_ttl_floor as s03
        v = s03.check(snap)
        assert len(v) == 1
        assert v[0].observed_state["kind"] == "missing"
        assert v[0].observed_state["redis_ttl_seconds"] == -2
        # The floor could not be derived from the slot; the row says so rather
        # than implying the agent cap was this slot's real bound.
        assert v[0].observed_state["floor_source"] == "unknown"
        assert v[0].observed_state["stored_timeout_seconds"] is None

    def test_fires_when_ttl_unset(self, canary_db, reload_canary):
        """Metadata HASH exists but no expire was set on it."""
        _add_agent(canary_db, "a1", timeout=60)
        reload_canary["redis"].zadd("agent:slots:a1", {"e1": self._now_score()})
        # Populate the HASH so FakeRedis.ttl returns -1 (exists, no TTL).
        reload_canary["redis"].hset("agent:slot:a1:e1", "started_at", "x")
        self._store_timeout(reload_canary["redis"], "a1", "e1", 60)
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s03_slot_ttl_floor as s03
        v = s03.check(snap)
        assert len(v) == 1
        assert v[0].observed_state["kind"] == "no_expiry"
        assert v[0].observed_state["redis_ttl_seconds"] == -1

    def test_explicit_short_timeout_below_agent_cap_does_not_fire(
        self, canary_db, reload_canary
    ):
        """ent#336 — THE regression this issue is about.

        Agent cap 3600 (floor 3900 under the old agent-cap rule), schedule
        timeout 2700 → slot TTL 3000. Correct by construction, and it paged
        critical on every normally-configured scheduled run before the fix.
        """
        _add_agent(canary_db, "a1", timeout=3600)
        reload_canary["redis"].zadd("agent:slots:a1", {"e1": self._now_score()})
        self._store_timeout(reload_canary["redis"], "a1", "e1", 2700)
        reload_canary["redis"].set_ttl("agent:slot:a1:e1", 3000)
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import s03_slot_ttl_floor as s03
        assert s03.check(snap) == []

    def test_unobservable_timeout_skips_rather_than_using_agent_cap(
        self, canary_db, reload_canary
    ):
        """A live TTL with no stored timeout must SKIP, not fall back.

        The fallback would re-arm the exact false positive above whenever the
        HASH expires between the two reads: TTL 3000 against an agent-cap floor
        of 3900 fires, while against the slot's real 2700 bound it does not.
        """
        _add_agent(canary_db, "a1", timeout=3600)
        reload_canary["redis"].zadd("agent:slots:a1", {"e1": self._now_score()})
        # TTL readable, `timeout_seconds` absent.
        reload_canary["redis"].set_ttl("agent:slot:a1:e1", 3000)
        snap = reload_canary["canary"].collect_snapshot()
        assert snap.agents[0].slot_timeouts == {}
        from canary.invariants import s03_slot_ttl_floor as s03
        assert s03.check(snap) == []

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
    # ent#337: the exec now emits one PID per line (empty output = no zombies),
    # not a count. See `_collect_zombie_counts` for why the dwell needs PIDs.
    def test_holds_when_no_zombies(self, canary_db, reload_canary, fake_docker):
        _add_agent(canary_db, "a1")
        fake_docker.add_container("agent-a1", exec_output="")
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import r01_no_zombie_claude as r01
        assert r01.check(snap) == []
        assert snap.zombie_counts == {"a1": 0}
        assert snap.zombie_pids == {"a1": set()}

    def test_collects_zombie_pids(self, canary_db, reload_canary, fake_docker):
        """A single observation is collected but does NOT fire (ent#337).

        Firing needs the dwell window; see the boundary suite in
        `tests/unit/test_ent337_r01_zombie_dwell.py`.
        """
        _add_agent(canary_db, "a1")
        fake_docker.add_container("agent-a1", exec_output="811\n902\n1043\n")
        snap = reload_canary["canary"].collect_snapshot()
        assert snap.zombie_counts == {"a1": 3}
        assert snap.zombie_pids == {"a1": {811, 902, 1043}}
        from canary.invariants import r01_no_zombie_claude as r01
        assert r01.check(snap) == []

    def test_unparseable_ps_output_is_unavailable_not_green(
        self, canary_db, reload_canary, fake_docker
    ):
        """A diagnostic on stdout must not read as 'no zombies'.

        `ps` absent on a minimal image would otherwise report a silent
        false-green on the one invariant that watches the process table.
        """
        _add_agent(canary_db, "a1")
        fake_docker.add_container("agent-a1", exec_output="sh: ps: not found")
        snap = reload_canary["canary"].collect_snapshot()
        assert "a1" not in snap.zombie_counts
        assert any("docker.exec[a1]" in s for s in snap.sources_unavailable)

    def test_per_container_exec_failure_does_not_kill_cycle(
        self, canary_db, reload_canary, fake_docker
    ):
        _add_agent(canary_db, "ok")
        _add_agent(canary_db, "broken")
        fake_docker.add_container("agent-ok", exec_output="")
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


# ---------------------------------------------------------------------------
# Invariant: E-06 no overdue next_run_at (#1472)
# ---------------------------------------------------------------------------

def _iso_z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class TestE06OverdueNextRun:
    """Enabled, non-deleted schedule whose next_run_at is >misfire-grace in the
    past — the "Next: Nd ago" projection the scheduler never advanced (#1472)."""

    def test_synthetic_overdue_fires_future_null_and_grace_hold(self):
        from datetime import timezone
        from canary.snapshot import Snapshot
        from canary.invariants import e06_no_overdue_next_run as e06

        now = datetime.utcnow()
        kiev_past = (
            now.replace(tzinfo=timezone.utc) - _td(hours=2)
        ).astimezone(timezone(_td(hours=3)))  # non-UTC render of a 2h-past instant
        rows = [
            {"schedule_id": "s-overdue-utc", "agent_name": "a", "next_run_at": _iso_z(now - _td(hours=2))},
            {"schedule_id": "s-future", "agent_name": "a", "next_run_at": _iso_z(now + _td(hours=1))},
            {"schedule_id": "s-within-grace", "agent_name": "a", "next_run_at": _iso_z(now - _td(minutes=30))},
            {"schedule_id": "s-null", "agent_name": "a", "next_run_at": None},
            {"schedule_id": "s-overdue-kiev", "agent_name": "a", "next_run_at": kiev_past.isoformat()},
        ]
        snap = Snapshot(snapshot_time=_iso_z(now), enabled_schedules=rows)
        fired = sorted(v.observed_state["schedule_id"] for v in e06.check(snap))
        assert fired == ["s-overdue-kiev", "s-overdue-utc"]

    def test_collector_predicate_and_end_to_end(self, canary_db, reload_canary):
        now = datetime.utcnow()
        _add_agent(canary_db, "a1")
        _add_schedule(canary_db, "sch-stale", "a1", _iso_z(now - _td(hours=2)))
        _add_schedule(canary_db, "sch-future", "a1", _iso_z(now + _td(hours=1)))
        _add_schedule(canary_db, "sch-disabled", "a1", _iso_z(now - _td(hours=2)), enabled=0)
        _add_schedule(canary_db, "sch-deleted", "a1", _iso_z(now - _td(hours=2)), deleted_at=_iso_z(now))

        snap = reload_canary["canary"].collect_snapshot()
        # collector predicate mirrors the scheduler: enabled = 1 AND deleted_at IS NULL
        collected = {s["schedule_id"] for s in snap.enabled_schedules}
        assert collected == {"sch-stale", "sch-future"}

        from canary.invariants import e06_no_overdue_next_run as e06
        fired = [v.observed_state["schedule_id"] for v in e06.check(snap)]
        assert fired == ["sch-stale"]


# ---------------------------------------------------------------------------
# Terminal-row collector (#1077 — shared by E-03 / G-03)
# ---------------------------------------------------------------------------


class TestTerminalRowCollector:
    def test_window_in_vs_out(self, canary_db, reload_canary):
        # Agent timeout 900s → window = 900 + 300 = 1200s (20 min).
        _add_agent(canary_db, "a1", timeout=900)
        now = datetime.utcnow()
        _add_execution(
            canary_db, "e-in", "a1", "success",
            started_at=_iso_z(now - _td(seconds=60)),
            completed_at=_iso_z(now),
        )
        _add_execution(
            canary_db, "e-out", "a1", "success",
            started_at=_iso_z(now - _td(hours=3)),
            completed_at=_iso_z(now - _td(hours=3) + _td(seconds=5)),
        )
        snap = reload_canary["canary"].collect_snapshot()
        ids = {r["id"] for r in snap.terminal_rows}
        assert "e-in" in ids
        assert "e-out" not in ids
        assert snap.sources_unavailable == []

    def test_null_completed_at_still_collected(self, canary_db, reload_canary):
        # Windowed on started_at, so a half-written row (NULL completed_at) is
        # still collected — E-03 must be able to see it.
        _add_agent(canary_db, "a1")
        now = datetime.utcnow()
        _add_execution(
            canary_db, "e-halfwritten", "a1", "failed",
            started_at=_iso_z(now - _td(seconds=30)),
            completed_at=None,
        )
        snap = reload_canary["canary"].collect_snapshot()
        rows = {r["id"]: r for r in snap.terminal_rows}
        assert "e-halfwritten" in rows
        assert rows["e-halfwritten"]["completed_at"] is None

    def test_skipped_status_excluded(self, canary_db, reload_canary):
        # A `skipped` row legitimately has NULL completed_at/duration_ms — it
        # must NOT be pulled into terminal_rows (else E-03 false-fires).
        _add_agent(canary_db, "a1")
        now = datetime.utcnow()
        _add_execution(
            canary_db, "e-skip", "a1", "skipped",
            started_at=_iso_z(now - _td(seconds=30)),
            completed_at=None, duration_ms=None,
        )
        _add_execution(
            canary_db, "e-ok", "a1", "success",
            started_at=_iso_z(now - _td(seconds=30)),
            completed_at=_iso_z(now),
        )
        snap = reload_canary["canary"].collect_snapshot()
        ids = {r["id"] for r in snap.terminal_rows}
        assert "e-ok" in ids
        assert "e-skip" not in ids
        from canary.invariants import e03_completed_rows_populated as e03
        assert e03.check(snap) == []

    def test_column_absent_skips_collection(self, monkeypatch):
        # An ABSENT completed_at/duration_ms column (minimal/older DDL) must
        # skip the source (recorded in `unavailable`) rather than default the
        # column to None and false-fire E-03 on every row.
        #
        # #1540: `_collect_terminal_rows` reads through the get_engine()/
        # DATABASE_URL seam and reflects the LIVE columns via
        # sqlalchemy.inspect(), so this drives the minimal DDL through the ENGINE
        # (point DATABASE_URL at the file), proving the inspect() column guard is
        # fail-open identically to the old raw PRAGMA guard.
        import tempfile as _tf

        db_file = _tf.NamedTemporaryFile(suffix="_ta.db", delete=False)
        db_file.close()
        c = sqlite3.connect(db_file.name)
        c.row_factory = sqlite3.Row
        c.executescript(
            """
            CREATE TABLE schedule_executions (
                id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL
            );
            """
        )
        c.execute(
            "INSERT INTO schedule_executions (id, agent_name, status, started_at) "
            "VALUES ('e1', 'a1', 'success', ?)",
            (_iso_z(datetime.utcnow()),),
        )
        c.commit()
        c.close()

        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.name}")
        import db.engine as engine_mod
        engine_mod.dispose_engines()

        from canary.snapshot import _collect_terminal_rows

        res = _collect_terminal_rows(1200)
        assert res["rows"] == []
        assert res["unavailable"] and (
            "completed_at" in res["unavailable"]
            or "duration_ms" in res["unavailable"]
        )
        engine_mod.dispose_engines()
        os.unlink(db_file.name)

    def test_cap_and_sampled_flag(self, canary_db, reload_canary, monkeypatch):
        _add_agent(canary_db, "a1")
        from canary import snapshot as snap_mod

        monkeypatch.setattr(snap_mod, "_TERMINAL_ROWS_CAP", 2)
        now = datetime.utcnow()
        for i in range(3):
            _add_execution(
                canary_db, f"e{i}", "a1", "success",
                started_at=_iso_z(now - _td(seconds=i)),
                completed_at=_iso_z(now),
            )
        res = snap_mod._collect_terminal_rows(3600)
        assert res["sampled"] is True
        assert len(res["rows"]) == 2


# ---------------------------------------------------------------------------
# E-03 — completed rows fully populated (#1077)
# ---------------------------------------------------------------------------


class TestInvariantE03:
    @staticmethod
    def _snap(rows):
        from canary.snapshot import Snapshot
        return Snapshot(snapshot_time="2026-05-18T12:00:00Z", terminal_rows=rows)

    def test_holds_when_completed_at_populated(self):
        snap = self._snap([
            {"id": "e1", "agent_name": "a1", "status": "success",
             "started_at": "2026-05-18T11:00:00Z",
             "completed_at": "2026-05-18T11:05:00Z", "duration_ms": 300000},
            # cancelled-from-queue: completed_at set, duration_ms NULL — healthy.
            {"id": "e2", "agent_name": "a1", "status": "cancelled",
             "started_at": "2026-05-18T11:00:00Z",
             "completed_at": "2026-05-18T11:00:01Z", "duration_ms": None},
        ])
        from canary.invariants import e03_completed_rows_populated as e03
        assert e03.check(snap) == []

    def test_fires_when_completed_at_null(self):
        snap = self._snap([
            {"id": "e-bad", "agent_name": "a1", "status": "success",
             "started_at": "2026-05-18T11:00:00Z",
             "completed_at": None, "duration_ms": None},
        ])
        from canary.invariants import e03_completed_rows_populated as e03
        v = e03.check(snap)
        assert len(v) == 1
        assert v[0].invariant_id == "E-03"
        assert v[0].tier == "A"
        assert v[0].severity == "major"
        assert v[0].observed_state["execution_id"] == "e-bad"
        assert v[0].observed_state["completed_at"] is None

    def test_null_duration_ms_alone_does_not_fire(self):
        # C1: duration_ms NULL is legitimate on queue-terminated rows; E-03
        # asserts completed_at only.
        snap = self._snap([
            {"id": "e-cancel", "agent_name": "a1", "status": "cancelled",
             "started_at": "2026-05-18T11:00:00Z",
             "completed_at": "2026-05-18T11:00:00Z", "duration_ms": None},
        ])
        from canary.invariants import e03_completed_rows_populated as e03
        assert e03.check(snap) == []

    def test_e2e_cancelled_from_queue_holds_clean(self, canary_db, reload_canary):
        # C1 regression guard through the REAL collector: a cancelled-from-queue
        # row (completed_at set, duration_ms NULL) must produce zero E-03
        # violations.
        _add_agent(canary_db, "a1")
        now = datetime.utcnow()
        _add_execution(
            canary_db, "e-cancel", "a1", "cancelled",
            started_at=_iso_z(now - _td(seconds=30)),
            completed_at=_iso_z(now), duration_ms=None,
        )
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import e03_completed_rows_populated as e03
        assert e03.check(snap) == []

    def test_e2e_half_written_fires(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        now = datetime.utcnow()
        _add_execution(
            canary_db, "e-half", "a1", "success",
            started_at=_iso_z(now - _td(seconds=30)), completed_at=None,
        )
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import e03_completed_rows_populated as e03
        fired = [x.observed_state["execution_id"] for x in e03.check(snap)]
        assert fired == ["e-half"]


# ---------------------------------------------------------------------------
# G-03 — clock sanity on terminal rows (#1077)
# ---------------------------------------------------------------------------


class TestInvariantG03:
    @staticmethod
    def _snap(rows):
        from canary.snapshot import Snapshot
        return Snapshot(snapshot_time="2026-05-18T12:00:00Z", terminal_rows=rows)

    def test_holds_when_started_le_completed(self):
        snap = self._snap([
            {"id": "e1", "agent_name": "a1", "status": "success",
             "started_at": "2026-05-18T11:00:00Z",
             "completed_at": "2026-05-18T11:05:00Z"},
        ])
        from canary.invariants import g03_clock_sanity as g03
        assert g03.check(snap) == []

    def test_fires_when_started_after_completed(self):
        snap = self._snap([
            {"id": "e-bad", "agent_name": "a1", "status": "success",
             "started_at": "2026-05-18T11:05:00Z",
             "completed_at": "2026-05-18T11:00:00Z"},
        ])
        from canary.invariants import g03_clock_sanity as g03
        v = g03.check(snap)
        assert len(v) == 1
        assert v[0].invariant_id == "G-03"
        assert v[0].tier == "A"
        assert v[0].severity == "minor"
        assert v[0].observed_state["execution_id"] == "e-bad"
        assert v[0].observed_state["skew_seconds"] == 300.0

    def test_sub_second_skew_does_not_fire(self):
        # 0.5s backward step is clock jitter (< 1s tolerance), not a bug.
        snap = self._snap([
            {"id": "e-jitter", "agent_name": "a1", "status": "success",
             "started_at": "2026-05-18T11:00:00.500000Z",
             "completed_at": "2026-05-18T11:00:00.000000Z"},
        ])
        from canary.invariants import g03_clock_sanity as g03
        assert g03.check(snap) == []

    def test_1474_naive_vs_z_compares_without_raising(self):
        # #1474: a legacy naive started_at beside a Z completed_at must compare
        # without raising and not false-fire when started <= completed.
        snap = self._snap([
            {"id": "e-mixed", "agent_name": "a1", "status": "success",
             "started_at": "2026-05-18T11:00:00",
             "completed_at": "2026-05-18T11:05:00Z"},
        ])
        from canary.invariants import g03_clock_sanity as g03
        assert g03.check(snap) == []

    def test_skips_null_completed_at(self):
        # E-03 owns the NULL-completed_at case; G-03 skips it (no double-fire).
        snap = self._snap([
            {"id": "e-null", "agent_name": "a1", "status": "failed",
             "started_at": "2026-05-18T11:00:00Z", "completed_at": None},
        ])
        from canary.invariants import g03_clock_sanity as g03
        assert g03.check(snap) == []

    def test_e2e_bad_clock_fires(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        now = datetime.utcnow()
        _add_execution(
            canary_db, "e-badclock", "a1", "success",
            started_at=_iso_z(now),
            completed_at=_iso_z(now - _td(seconds=10)),
        )
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import g03_clock_sanity as g03
        fired = [x.observed_state["execution_id"] for x in g03.check(snap)]
        assert fired == ["e-badclock"]

    def test_e2e_sub_second_skew_does_not_fire(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        now = datetime.utcnow()
        _add_execution(
            canary_db, "e-jitter", "a1", "success",
            started_at=_iso_z(now),
            completed_at=_iso_z(now - _td(milliseconds=500)),
        )
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import g03_clock_sanity as g03
        assert g03.check(snap) == []


# ---------------------------------------------------------------------------
# E-04 — queued rows carry valid backlog metadata (#1077)
# ---------------------------------------------------------------------------


class TestInvariantE04:
    @staticmethod
    def _snap(queued_meta):
        """One-agent snapshot whose queued_exec_ids == queued_meta.keys()."""
        from canary.snapshot import Snapshot, AgentSnapshot
        return Snapshot(
            snapshot_time="2026-05-18T12:00:00Z",
            agents=[
                AgentSnapshot(
                    name="a1",
                    is_system=False,
                    max_parallel=3,
                    execution_timeout_seconds=900,
                    queued_exec_ids=set(queued_meta.keys()),
                    queued_meta=queued_meta,
                )
            ],
        )

    def test_holds_when_queued_at_and_json_metadata_present(self):
        snap = self._snap({
            "e1": {"queued_at": "2026-05-18T11:00:00Z",
                   "backlog_metadata": '{"trigger": "schedule", "request_id": "r-1"}'},
        })
        from canary.invariants import e04_queued_rows_have_metadata as e04
        assert e04.check(snap) == []

    def test_fires_when_queued_at_null(self):
        snap = self._snap({
            "e-bad": {"queued_at": None,
                      "backlog_metadata": '{"trigger": "schedule"}'},
        })
        from canary.invariants import e04_queued_rows_have_metadata as e04
        v = e04.check(snap)
        assert len(v) == 1
        assert v[0].invariant_id == "E-04"
        assert v[0].tier == "A"
        assert v[0].severity == "major"
        assert v[0].observed_state["execution_id"] == "e-bad"
        assert v[0].observed_state["reason"] == "queued_at_null"

    def test_fires_when_backlog_metadata_null(self):
        snap = self._snap({
            "e-bad": {"queued_at": "2026-05-18T11:00:00Z",
                      "backlog_metadata": None},
        })
        from canary.invariants import e04_queued_rows_have_metadata as e04
        v = e04.check(snap)
        assert len(v) == 1
        assert v[0].observed_state["reason"] == "backlog_metadata_null"

    def test_fires_when_backlog_metadata_not_json(self):
        snap = self._snap({
            "e-bad": {"queued_at": "2026-05-18T11:00:00Z",
                      "backlog_metadata": "not-a-json-blob {oops"},
        })
        from canary.invariants import e04_queued_rows_have_metadata as e04
        v = e04.check(snap)
        assert len(v) == 1
        assert v[0].observed_state["reason"] == "backlog_metadata_invalid_json"
        # SECURITY: the raw (malformed) metadata value must NEVER be echoed.
        blob = json.dumps(v[0].observed_state) + (v[0].signal_query or "")
        assert "not-a-json-blob" not in blob

    def test_skips_eid_missing_from_queued_meta(self):
        # Older-image snapshot: eid queued but no metadata observed → skip
        # (fail-open), don't fire.
        from canary.snapshot import Snapshot, AgentSnapshot
        snap = Snapshot(
            snapshot_time="2026-05-18T12:00:00Z",
            agents=[
                AgentSnapshot(
                    name="a1", is_system=False, max_parallel=3,
                    execution_timeout_seconds=900,
                    queued_exec_ids={"e-legacy"},
                    queued_meta={},  # column absent → no entry
                )
            ],
        )
        from canary.invariants import e04_queued_rows_have_metadata as e04
        assert e04.check(snap) == []

    def test_e2e_valid_queued_row_holds_clean(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        _add_execution(
            canary_db, "e-q", "a1", "queued",
            queued_at="2026-05-18T10:00:00Z",
            backlog_metadata='{"trigger": "schedule", "request_id": "r-1"}',
        )
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import e04_queued_rows_have_metadata as e04
        assert e04.check(snap) == []

    def test_e2e_malformed_queued_metadata_fires(self, canary_db, reload_canary):
        _add_agent(canary_db, "a1")
        # NULL queued_at
        _add_execution(
            canary_db, "e-noqa", "a1", "queued",
            queued_at=None, backlog_metadata='{"ok": true}',
        )
        # NULL backlog_metadata
        _add_execution(
            canary_db, "e-nometa", "a1", "queued",
            queued_at="2026-05-18T10:00:00Z", backlog_metadata=None,
        )
        # Non-JSON backlog_metadata
        _add_execution(
            canary_db, "e-badjson", "a1", "queued",
            queued_at="2026-05-18T10:00:00Z", backlog_metadata="{broken",
        )
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import e04_queued_rows_have_metadata as e04
        fired = {v.observed_state["execution_id"]: v.observed_state["reason"]
                 for v in e04.check(snap)}
        assert fired == {
            "e-noqa": "queued_at_null",
            "e-nometa": "backlog_metadata_null",
            "e-badjson": "backlog_metadata_invalid_json",
        }


# ---------------------------------------------------------------------------
# G-04 — no raw credentials in backlog metadata (#1077)
# ---------------------------------------------------------------------------


class TestInvariantG04:
    @staticmethod
    def _snap(queued_meta):
        from canary.snapshot import Snapshot, AgentSnapshot
        return Snapshot(
            snapshot_time="2026-05-18T12:00:00Z",
            agents=[
                AgentSnapshot(
                    name="a1",
                    is_system=False,
                    max_parallel=3,
                    execution_timeout_seconds=900,
                    queued_exec_ids=set(queued_meta.keys()),
                    queued_meta=queued_meta,
                )
            ],
        )

    def test_holds_when_no_secret_pattern(self):
        # Benign metadata whose text includes "task-" (contains "sk-") must NOT
        # false-fire — the patterns are word-boundary anchored.
        snap = self._snap({
            "e1": {"queued_at": "2026-05-18T11:00:00Z",
                   "backlog_metadata":
                       '{"trigger": "schedule", "task-name": "risky-task-42"}'},
        })
        from canary.invariants import g04_no_creds_in_backlog_metadata as g04
        assert g04.check(snap) == []

    def test_fires_on_github_pat(self):
        secret = "ghp_ABCDEFghij0123456789KLMNOPqrstuv"
        snap = self._snap({
            "e-leak": {"queued_at": "2026-05-18T11:00:00Z",
                       "backlog_metadata": '{"token": "%s"}' % secret},
        })
        from canary.invariants import g04_no_creds_in_backlog_metadata as g04
        v = g04.check(snap)
        assert len(v) == 1
        assert v[0].invariant_id == "G-04"
        assert v[0].tier == "A"
        assert v[0].severity == "critical"
        assert v[0].observed_state["execution_id"] == "e-leak"
        assert v[0].observed_state["matched_pattern"] == "github_pat"
        # SECURITY: the secret value must appear NOWHERE in the violation record.
        blob = json.dumps(v[0].observed_state) + (v[0].signal_query or "")
        assert secret not in blob
        assert "ABCDEFghij" not in blob

    def test_fires_on_openai_and_slack_and_aws(self):
        cases = {
            "e-openai": "sk-proj-ABCDEFGHIJ0123456789abcdefghij",
            "e-slack": "xoxb-1234-5678-abcdEFGHijklMNOP",
            "e-aws": "AKIAIOSFODNN7EXAMPLE",
        }
        meta = {
            eid: {"queued_at": "2026-05-18T11:00:00Z",
                  "backlog_metadata": '{"cred": "%s"}' % val}
            for eid, val in cases.items()
        }
        snap = self._snap(meta)
        from canary.invariants import g04_no_creds_in_backlog_metadata as g04
        v = g04.check(snap)
        assert len(v) == 3
        # No secret bytes leak into any record.
        for viol in v:
            blob = json.dumps(viol.observed_state) + (viol.signal_query or "")
            for secret in cases.values():
                assert secret not in blob

    def test_skips_null_metadata(self):
        # E-04 owns the NULL case; G-04 has nothing to scan.
        snap = self._snap({
            "e-null": {"queued_at": "2026-05-18T11:00:00Z",
                       "backlog_metadata": None},
        })
        from canary.invariants import g04_no_creds_in_backlog_metadata as g04
        assert g04.check(snap) == []

    def test_e2e_credential_in_backlog_metadata_fires_no_leak(
        self, canary_db, reload_canary
    ):
        _add_agent(canary_db, "a1")
        secret = "ghp_zzzzYYYYxxxx0000111122223333WWWW"
        _add_execution(
            canary_db, "e-leak", "a1", "queued",
            queued_at="2026-05-18T10:00:00Z",
            backlog_metadata='{"github_pat": "%s"}' % secret,
        )
        snap = reload_canary["canary"].collect_snapshot()
        from canary.invariants import g04_no_creds_in_backlog_metadata as g04
        v = g04.check(snap)
        assert [x.observed_state["execution_id"] for x in v] == ["e-leak"]
        assert v[0].observed_state["matched_pattern"] == "github_pat"
        # SECURITY end-to-end: the persisted violation carries no secret bytes.
        blob = json.dumps(v[0].observed_state) + (v[0].signal_query or "")
        assert secret not in blob


# ---------------------------------------------------------------------------
# Issue #1540 — SQL-tier collector reads route through the get_engine()/
# DATABASE_URL seam (un-blinding on PostgreSQL).
#
# The `canary_db_split` fixture is the raw≠engine proof harness: writing to the
# ENGINE file only proves the collectors read the engine seam; writing to the
# RAW file only proves `db.connection` is no longer read. These are SQLite-on-
# both-sides (a different *file*, not psycopg2), so `test_pg_*` (env-gated on
# TRINITY_TEST_PG_URL) is the required real-Postgres gate for the dialect path.
# ---------------------------------------------------------------------------


class TestIssue1540EngineSeam:
    # -- §6a: the un-blinding gate (the core proof) -----------------------

    def test_engine_only_agents_populate_snapshot(
        self, canary_db_split, reload_canary_split
    ):
        """THE un-blinding proof: agents written to the ENGINE file only must
        populate snap.known_agents/snap.agents. Pre-#1540 `_collect_known_agents`
        read the RAW file → empty → the whole per-agent loop
        (S-01/E-05/B-02/E-04/G-04 and B-01) went vacuously green on Postgres.
        """
        _raw_path, engine_path = canary_db_split
        _add_agent(engine_path, "a1")
        _add_execution(engine_path, "e1", "a1", "running")

        snap = reload_canary_split["canary"].collect_snapshot()
        assert snap.known_agents == {"a1"}
        assert [a.name for a in snap.agents] == ["a1"]
        assert snap.agents[0].running_exec_ids == {"e1"}

    def test_raw_only_rows_ignored(self, canary_db_split, reload_canary_split):
        """Negative proof: rows written to the RAW file only are IGNORED — the
        collectors read the engine, not `db.connection`. If a collector still
        read raw, the ghost agent + orphan row would leak into the snapshot.
        """
        raw_path, _engine_path = canary_db_split
        _add_agent(raw_path, "raw-ghost")
        _add_orphan_sharing(raw_path, "orphan-in-raw")

        snap = reload_canary_split["canary"].collect_snapshot()
        assert snap.known_agents == set()
        assert snap.agents == []
        assert snap.orphan_refs == []

    def test_s01_fires_on_engine_only_mismatch(
        self, canary_db_split, reload_canary_split
    ):
        """AC: a synthetic slot–row mismatch on the engine backend fires S-01.
        Engine running row + NO matching Redis slot → `in_sql_only`.
        """
        _raw_path, engine_path = canary_db_split
        _add_agent(engine_path, "a1")
        # Default started_at is old (past the S-01 grace) and there is no Redis
        # slot for e1 → in_sql_only.
        _add_execution(engine_path, "e1", "a1", "running")

        snap = reload_canary_split["canary"].collect_snapshot()
        from canary.invariants import s01_slot_row_bijection as s01
        violations = s01.check(snap)
        assert len(violations) == 1
        assert violations[0].observed_state["in_sql_only"] == ["e1"]

    def test_s01_clean_engine_state_does_not_fire(
        self, canary_db_split, reload_canary_split
    ):
        """AC: a clean slot⇄row bijection on the engine backend does NOT fire."""
        _raw_path, engine_path = canary_db_split
        _add_agent(engine_path, "a1")
        _add_execution(engine_path, "e1", "a1", "running")
        reload_canary_split["redis"].zadd("agent:slots:a1", {"e1": 1.0})

        snap = reload_canary_split["canary"].collect_snapshot()
        from canary.invariants import s01_slot_row_bijection as s01
        assert s01.check(snap) == []

    def test_l03_fires_on_engine_only_orphan(
        self, canary_db_split, reload_canary_split
    ):
        """Fleet-wide read un-blinds: an orphan agent_sharing row in the ENGINE
        file (agent absent from ENGINE agent_ownership) fires L-03.
        """
        _raw_path, engine_path = canary_db_split
        _add_agent(engine_path, "a1")
        _add_orphan_sharing(engine_path, "ghost")

        snap = reload_canary_split["canary"].collect_snapshot()
        assert {r.referenced_agent_name for r in snap.orphan_refs} == {"ghost"}
        from canary.invariants import l03_delete_cascades as l03
        assert len(l03.check(snap)) >= 1

    def test_e05_fires_on_engine_only_dispatched_row(
        self, canary_db_split, reload_canary_split
    ):
        """Fleet-wide read un-blinds: an old ENGINE running row with no session
        id fires E-05.
        """
        _raw_path, engine_path = canary_db_split
        _add_agent(engine_path, "a1")
        _add_execution(engine_path, "e-old", "a1", "running")

        snap = reload_canary_split["canary"].collect_snapshot()
        from canary.invariants import e05_dispatched_rows_have_session as e05
        violations = e05.check(snap)
        assert [x.observed_state["execution_id"] for x in violations] == ["e-old"]

    # -- §6f: composite-PK agent_tags orphan scan (the HIGH regression) ----

    def test_orphan_scan_agent_tags_composite_pk(self, canary_db, reload_canary):
        """L-03 orphan scan over the composite-PK `agent_tags` table (no `id`
        column) yields a synthetic `agent_tags-row` row_id. Direct regression for
        the `KeyError('id')` a reflected-PK port would have crashed on — the PK is
        derived from the STATIC db/tables.py Table, not reflection.
        """
        _add_agent(canary_db, "real")
        c = _conn(canary_db)
        c.execute(
            "INSERT INTO agent_tags (agent_name, tag) VALUES (?, ?)",
            ("ghost-tagged", "priority"),
        )
        c.commit()
        c.close()

        snap = reload_canary["canary"].collect_snapshot()
        tag_refs = [r for r in snap.orphan_refs if r.table == "agent_tags"]
        assert len(tag_refs) == 1
        assert tag_refs[0].referenced_agent_name == "ghost-tagged"
        assert tag_refs[0].row_id == "agent_tags-row"

    # -- §6c: minimal-DDL fail-open on the engine (inspect() column guard) --

    def test_executions_minimal_ddl_fail_open(self, monkeypatch):
        """`_collect_executions` reflects LIVE columns via `inspect()`: a minimal
        DDL lacking claude_session_id / lease_expires_at / queued_at /
        backlog_metadata still collects (fail-open) — session=None, no leased
        flag, empty queued_meta — instead of erroring on an absent column. This
        is the older-image path, now on the engine seam.
        """
        import tempfile as _tf

        db_file = _tf.NamedTemporaryFile(suffix="_min.db", delete=False)
        db_file.close()
        c = sqlite3.connect(db_file.name)
        c.executescript(
            """
            CREATE TABLE schedule_executions (
                id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT
            );
            """
        )
        c.execute(
            "INSERT INTO schedule_executions (id, agent_name, status, started_at) "
            "VALUES ('r1', 'a1', 'running', '2026-04-30T00:00:00Z'), "
            "('q1', 'a1', 'queued', '2026-04-30T00:00:00Z')"
        )
        c.commit()
        c.close()

        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.name}")
        import db.engine as engine_mod
        engine_mod.dispose_engines()

        from canary.snapshot import _collect_executions
        out = _collect_executions("a1")
        assert out["running"] == {"r1"}
        assert out["queued"] == {"q1"}
        # Absent claude_session_id → None (E-05 grace path), not a KeyError.
        assert out["claude_session_ids"] == {"r1": None}
        assert out["lease_expires_at"] == {"r1": None}
        # Absent queued_at/backlog_metadata → empty (E-04/G-04 older-image skip).
        assert out["queued_meta"] == {}

        engine_mod.dispose_engines()
        os.unlink(db_file.name)

    # -- §6g: engine-down preserves the sqlite.* label skip contract --------

    def test_engine_down_preserves_labels_and_l03_e02_skip(
        self, canary_db, reload_canary, monkeypatch
    ):
        """If the orphan/terminal SQL reads fail (engine unreachable mid-cycle),
        the `sources_unavailable` entries keep their historical `sqlite.*`
        prefixes VERBATIM and L-03 / E-02 SKIP (fail-open) on that prefix — never
        false-fire against empty data. Pins the label→skip contract (#1540 §4g):
        a cosmetic `sqlite.*`→`engine.*` rename would silently disable the skip.
        """
        _add_agent(canary_db, "a1")
        from canary import snapshot as snap_mod

        def _boom(*a, **kw):
            raise RuntimeError("engine unreachable")

        monkeypatch.setattr(snap_mod, "_collect_orphan_refs", _boom)
        monkeypatch.setattr(snap_mod, "_collect_terminal_executions", _boom)

        snap = reload_canary["canary"].collect_snapshot()
        assert any(
            s.startswith("sqlite.orphan_refs") for s in snap.sources_unavailable
        )
        assert any(
            s.startswith("sqlite.terminal_executions")
            for s in snap.sources_unavailable
        )
        results = reload_canary["canary"].run_invariants(snap)
        assert results["L-03"] == []
        assert results["E-02"] == []

    # -- §6h: static import-guard (silent re-blinding tripwire) -------------

    def test_snapshot_does_not_import_db_connection(self):
        """After #1540 the collector reads ONLY through the engine seam — it must
        not import `db.connection`. Pins the migration so a future edit can't
        quietly re-introduce a raw-sqlite read that would re-blind Postgres.
        Matches on import STATEMENTS (not docstring mentions).
        """
        import pathlib
        import re

        import canary.snapshot as snap_mod

        src = pathlib.Path(snap_mod.__file__).read_text()
        offending = [
            line
            for line in src.splitlines()
            if re.match(
                r"\s*(from\s+db\.connection\s+import|import\s+db\.connection)", line
            )
        ]
        assert offending == [], (
            f"canary/snapshot.py must not import db.connection (#1540): {offending}"
        )

    # -- §6d: real-PostgreSQL un-blinding (env-gated required pre-merge gate) --

    @pytest.mark.skipif(
        not os.getenv("TRINITY_TEST_PG_URL"),
        reason="requires a real PostgreSQL (set TRINITY_TEST_PG_URL)",
    )
    def test_pg_unblinding_and_sources_clean(
        self, fake_redis, fake_docker, monkeypatch
    ):
        """AC 'on PG': drive `collect_snapshot()` against a REAL PostgreSQL and
        assert (1) no collector silently errored (`sources_unavailable == []`) —
        proving the psycopg2 dialect renders every ported statement, including
        `inspect()` information_schema reflection, `.notin_()` expansion, and the
        `text()` orphan filters — and (2) the un-blinding holds: agents populate,
        a synthetic slot–row mismatch fires S-01, an orphan row fires L-03.

        `canary_db_split` is SQLite-on-both-sides (a different *file*, not
        psycopg2), so it proves the read-*routing* moved off `db.connection` but
        never exercises PG's dialect — this test does. Run before PR-ready:
          docker run -d -e POSTGRES_PASSWORD=x -p 5433:5432 postgres:16-alpine
          TRINITY_TEST_PG_URL=postgresql+psycopg2://postgres:x@localhost:5433/postgres \\
            .venv-test/bin/python -m pytest tests/test_canary_invariants.py -k pg
        """
        pg_url = os.environ["TRINITY_TEST_PG_URL"]
        monkeypatch.setenv("DATABASE_URL", pg_url)
        import db.engine as engine_mod
        engine_mod.dispose_engines()

        from db.tables import (
            access_requests,
            agent_ownership,
            agent_public_links,
            agent_reports,
            agent_schedules,
            agent_shared_files,
            agent_sharing,
            agent_skills,
            agent_tags,
            chat_sessions,
            mcp_api_keys,
            operator_queue,
            schedule_executions,
        )

        canary_tables = [
            agent_ownership,
            schedule_executions,
            agent_schedules,
            agent_sharing,
            chat_sessions,
            agent_skills,
            agent_tags,
            agent_shared_files,
            agent_public_links,
            operator_queue,
            access_requests,
            agent_reports,
            mcp_api_keys,
        ]
        engine = engine_mod.get_engine()
        # Clean slate: (re)create just the canary tables on PG (no ForeignKeys in
        # db/tables.py → the subset is self-contained).
        for tbl in reversed(canary_tables):
            tbl.drop(engine, checkfirst=True)
        for tbl in canary_tables:
            tbl.create(engine, checkfirst=True)
        try:
            with engine.begin() as conn:
                conn.execute(
                    agent_ownership.insert().values(
                        agent_name="a1",
                        owner_id=1,
                        is_system=0,
                        max_parallel_tasks=3,
                        execution_timeout_seconds=900,
                    )
                )
                # running row, no matching Redis slot, old started_at → S-01 fires.
                conn.execute(
                    schedule_executions.insert().values(
                        id="e1",
                        agent_name="a1",
                        status="running",
                        started_at="2026-04-30T00:00:00Z",
                        message="",
                        triggered_by="test",
                    )
                )
                # orphan sharing row referencing a non-existent agent → L-03 fires.
                conn.execute(
                    agent_sharing.insert().values(
                        agent_name="ghost",
                        shared_with_email="g@example.com",
                        shared_by_id=1,
                        created_at="2026-04-30T00:00:00Z",
                    )
                )

            bundle = _reload_canary_with_temp_db(fake_redis, monkeypatch)
            snap = bundle["canary"].collect_snapshot()

            assert snap.sources_unavailable == [], (
                f"a collector silently errored on PostgreSQL: "
                f"{snap.sources_unavailable}"
            )
            assert snap.known_agents == {"a1"}

            from canary.invariants import l03_delete_cascades as l03
            from canary.invariants import s01_slot_row_bijection as s01

            assert [v.observed_state["in_sql_only"] for v in s01.check(snap)] == [
                ["e1"]
            ]
            assert any(
                r.referenced_agent_name == "ghost" for r in snap.orphan_refs
            )
            assert len(l03.check(snap)) >= 1
        finally:
            for tbl in reversed(canary_tables):
                tbl.drop(engine, checkfirst=True)
            engine_mod.dispose_engines()
