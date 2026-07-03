"""
Platform Audit Log unit tests (test_audit_log_unit.py).

Issue #20 / SEC-001 Phase 1. Covers:
- PlatformAuditOperations: insert, query (with filters), count, single-fetch, range, stats
- Append-only enforcement: SQLite triggers block UPDATE and DELETE within retention
- PlatformAuditService: actor resolution, event_id generation, JSON details serialization,
  and the "audit failures must never raise" contract

These tests run with an in-memory-style temporary SQLite DB that is wired into
the existing `db.connection` module via monkeypatch — no live backend required.
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import types
from datetime import datetime
from unittest.mock import MagicMock

import pytest
import sqlalchemy.exc

# Add backend to path for direct imports.
_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "backend")
)
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)

# Note: `utils.helpers` is preloaded as the real backend module by
# tests/conftest.py (_preload_backend_helpers_submodule). The tests/conftest.py
# autouse fixture also restores it after every test (Issue #762).


# ---------------------------------------------------------------------------
# Fixture: temporary SQLite DB with audit_log table + triggers
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_db(monkeypatch):
    """Create an isolated audit_log DB and patch db.connection to use it."""
    db_file = tempfile.NamedTemporaryFile(suffix="_audit_test.db", delete=False)
    db_file.close()
    db_path = db_file.name

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE NOT NULL,
            event_type TEXT NOT NULL,
            event_action TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id TEXT,
            actor_email TEXT,
            actor_ip TEXT,
            mcp_key_id TEXT,
            mcp_key_name TEXT,
            mcp_scope TEXT,
            target_type TEXT,
            target_id TEXT,
            timestamp TEXT NOT NULL,
            details TEXT,
            request_id TEXT,
            source TEXT NOT NULL,
            endpoint TEXT,
            previous_hash TEXT,
            entry_hash TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    cursor.execute(
        """
        CREATE TRIGGER audit_log_no_update
        BEFORE UPDATE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'Audit log entries cannot be modified');
        END
        """
    )
    cursor.execute(
        """
        CREATE TRIGGER audit_log_no_delete
        BEFORE DELETE ON audit_log
        WHEN OLD.timestamp > datetime('now', '-365 days')
        BEGIN
            SELECT RAISE(ABORT, 'Audit log entries cannot be deleted within retention period');
        END
        """
    )
    conn.commit()
    conn.close()

    # Build a fake db.connection module that hands out connections to our temp DB.
    # Kept for any not-yet-converted module that still imports the legacy seam;
    # harmless for the converted db.audit, which routes via get_engine().
    class _ConnContext:
        def __enter__(self):
            self._conn = sqlite3.connect(db_path)
            self._conn.row_factory = sqlite3.Row
            return self._conn

        def __exit__(self, exc_type, exc_val, exc_tb):
            self._conn.close()

    fake_module = types.ModuleType("db.connection")
    fake_module.get_db_connection = lambda: _ConnContext()
    monkeypatch.setitem(sys.modules, "db.connection", fake_module)

    # Route the SQLAlchemy engine seam (#300) at the same temp file. db.audit
    # is converted and reads DATABASE_URL via get_engine(); the engine cache is
    # keyed by URL, so dispose after setenv so the temp file's engine is the one
    # created (and dispose again at teardown to drop it).
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    import db.engine as engine_mod
    engine_mod.dispose_engines()

    yield db_path

    engine_mod.dispose_engines()
    os.unlink(db_path)


@pytest.fixture
def audit_ops(audit_db, monkeypatch):
    """Fresh PlatformAuditOperations instance using the temp DB."""
    # Force reimport so the new db.connection stub is picked up.
    # monkeypatch.delitem auto-restores db.audit after the test (Issue #762).
    monkeypatch.delitem(sys.modules, "db.audit", raising=False)
    from db.audit import PlatformAuditOperations

    return PlatformAuditOperations()


# Override the backend-requiring autouse fixture from package conftest.
@pytest.fixture(scope="session")
def api_client():
    yield None


@pytest.fixture(autouse=True)
def cleanup_after_test():
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    event_id="evt-1",
    event_type="agent_lifecycle",
    event_action="create",
    actor_type="user",
    actor_id="42",
    timestamp="2026-04-14T10:00:00Z",
    source="api",
    **kwargs,
):
    base = {
        "event_id": event_id,
        "event_type": event_type,
        "event_action": event_action,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "timestamp": timestamp,
        "source": source,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Schema / immutability
# ---------------------------------------------------------------------------


def test_insert_and_fetch(audit_ops):
    audit_ops.create_audit_entry(_entry(event_id="evt-a", actor_email="alice@example.com"))
    fetched = audit_ops.get_audit_entry("evt-a")
    assert fetched is not None
    assert fetched["event_id"] == "evt-a"
    assert fetched["actor_email"] == "alice@example.com"
    assert fetched["event_type"] == "agent_lifecycle"


def test_insert_with_json_details(audit_ops):
    audit_ops.create_audit_entry(
        _entry(event_id="evt-json", details=json.dumps({"agent": "oracle", "tags": ["alpha"]}))
    )
    fetched = audit_ops.get_audit_entry("evt-json")
    assert fetched["details"] == {"agent": "oracle", "tags": ["alpha"]}


def test_unique_event_id_enforced(audit_ops):
    audit_ops.create_audit_entry(_entry(event_id="dup"))
    # The write path now goes through SQLAlchemy Core (#300), which wraps the
    # underlying sqlite3.IntegrityError (UNIQUE constraint) in
    # sqlalchemy.exc.IntegrityError. Assert the wrapper and that the chained
    # cause is still the sqlite UNIQUE violation — no weakening.
    with pytest.raises(sqlalchemy.exc.IntegrityError) as exc_info:
        audit_ops.create_audit_entry(_entry(event_id="dup"))
    assert isinstance(exc_info.value.orig, sqlite3.IntegrityError)
    assert "UNIQUE constraint failed" in str(exc_info.value.orig)


def test_update_blocked_by_trigger(audit_ops, audit_db):
    audit_ops.create_audit_entry(_entry(event_id="evt-immut"))
    conn = sqlite3.connect(audit_db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="cannot be modified"):
            conn.execute(
                "UPDATE audit_log SET event_action = 'tampered' WHERE event_id = ?",
                ("evt-immut",),
            )
            conn.commit()
    finally:
        conn.close()


def test_delete_within_retention_blocked(audit_ops, audit_db):
    audit_ops.create_audit_entry(_entry(event_id="evt-recent"))
    conn = sqlite3.connect(audit_db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="retention period"):
            conn.execute("DELETE FROM audit_log WHERE event_id = ?", ("evt-recent",))
            conn.commit()
    finally:
        conn.close()


def test_delete_old_entry_allowed(audit_ops, audit_db):
    audit_ops.create_audit_entry(
        _entry(event_id="evt-old", timestamp="2024-01-01T00:00:00Z")
    )
    conn = sqlite3.connect(audit_db)
    try:
        conn.execute("DELETE FROM audit_log WHERE event_id = ?", ("evt-old",))
        conn.commit()
    finally:
        conn.close()
    assert audit_ops.get_audit_entry("evt-old") is None


# ---------------------------------------------------------------------------
# Query filters and pagination
# ---------------------------------------------------------------------------


def test_filter_by_event_type(audit_ops):
    audit_ops.create_audit_entry(_entry(event_id="a", event_type="agent_lifecycle"))
    audit_ops.create_audit_entry(_entry(event_id="b", event_type="authentication"))
    audit_ops.create_audit_entry(_entry(event_id="c", event_type="agent_lifecycle"))

    rows = audit_ops.get_audit_entries(event_type="agent_lifecycle")
    assert {r["event_id"] for r in rows} == {"a", "c"}


def test_filter_by_actor(audit_ops):
    audit_ops.create_audit_entry(_entry(event_id="u1", actor_type="user", actor_id="1"))
    audit_ops.create_audit_entry(_entry(event_id="u2", actor_type="user", actor_id="2"))
    audit_ops.create_audit_entry(_entry(event_id="a1", actor_type="agent", actor_id="oracle"))

    assert {r["event_id"] for r in audit_ops.get_audit_entries(actor_type="user")} == {"u1", "u2"}
    assert {r["event_id"] for r in audit_ops.get_audit_entries(actor_id="oracle")} == {"a1"}


def test_filter_by_target(audit_ops):
    audit_ops.create_audit_entry(_entry(event_id="t1", target_type="agent", target_id="oracle"))
    audit_ops.create_audit_entry(_entry(event_id="t2", target_type="agent", target_id="researcher"))
    audit_ops.create_audit_entry(_entry(event_id="t3", target_type="user", target_id="42"))

    rows = audit_ops.get_audit_entries(target_type="agent", target_id="oracle")
    assert [r["event_id"] for r in rows] == ["t1"]


def test_filter_by_time_range(audit_ops):
    audit_ops.create_audit_entry(_entry(event_id="early", timestamp="2026-01-01T00:00:00Z"))
    audit_ops.create_audit_entry(_entry(event_id="middle", timestamp="2026-02-01T00:00:00Z"))
    audit_ops.create_audit_entry(_entry(event_id="late", timestamp="2026-03-01T00:00:00Z"))

    rows = audit_ops.get_audit_entries(
        start_time="2026-01-15T00:00:00Z", end_time="2026-02-15T00:00:00Z"
    )
    assert [r["event_id"] for r in rows] == ["middle"]


def test_results_ordered_newest_first(audit_ops):
    audit_ops.create_audit_entry(_entry(event_id="old", timestamp="2026-01-01T00:00:00Z"))
    audit_ops.create_audit_entry(_entry(event_id="new", timestamp="2026-04-01T00:00:00Z"))
    audit_ops.create_audit_entry(_entry(event_id="mid", timestamp="2026-02-01T00:00:00Z"))

    rows = audit_ops.get_audit_entries()
    assert [r["event_id"] for r in rows] == ["new", "mid", "old"]


def test_pagination(audit_ops):
    for i in range(10):
        audit_ops.create_audit_entry(
            _entry(event_id=f"p{i}", timestamp=f"2026-04-{i+1:02d}T00:00:00Z")
        )
    page1 = audit_ops.get_audit_entries(limit=3, offset=0)
    page2 = audit_ops.get_audit_entries(limit=3, offset=3)
    assert len(page1) == 3 and len(page2) == 3
    assert {r["event_id"] for r in page1}.isdisjoint({r["event_id"] for r in page2})


def test_count_independent_of_pagination(audit_ops):
    for i in range(5):
        audit_ops.create_audit_entry(_entry(event_id=f"c{i}"))
    assert audit_ops.count_audit_entries() == 5
    assert audit_ops.count_audit_entries(event_type="agent_lifecycle") == 5
    assert audit_ops.count_audit_entries(event_type="authentication") == 0


def test_get_audit_entries_range(audit_ops):
    for i in range(5):
        audit_ops.create_audit_entry(_entry(event_id=f"r{i}"))
    rows = audit_ops.get_audit_entries_range(2, 4)
    assert [r["id"] for r in rows] == [2, 3, 4]


def test_stats_aggregation(audit_ops):
    audit_ops.create_audit_entry(_entry(event_id="s1", event_type="agent_lifecycle", actor_type="user"))
    audit_ops.create_audit_entry(_entry(event_id="s2", event_type="agent_lifecycle", actor_type="user"))
    audit_ops.create_audit_entry(_entry(event_id="s3", event_type="authentication", actor_type="user"))
    audit_ops.create_audit_entry(_entry(event_id="s4", event_type="mcp_operation", actor_type="mcp_client"))

    stats = audit_ops.get_audit_stats()
    assert stats["total"] == 4
    assert stats["by_event_type"]["agent_lifecycle"] == 2
    assert stats["by_event_type"]["authentication"] == 1
    assert stats["by_actor_type"]["user"] == 3
    assert stats["by_actor_type"]["mcp_client"] == 1


def test_missing_entry_returns_none(audit_ops):
    assert audit_ops.get_audit_entry("nonexistent") is None


# ---------------------------------------------------------------------------
# PlatformAuditService
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_service(audit_db, monkeypatch, audit_ops):
    """PlatformAuditService wired to the temp DB via a fake `database.db`."""
    # Reimport to pick up the temp DB.
    # monkeypatch.delitem auto-restores after the test (Issue #762).
    monkeypatch.delitem(sys.modules, "services.platform_audit_service", raising=False)

    fake_db_module = types.ModuleType("database")
    fake_db = MagicMock()
    fake_db.create_audit_entry = audit_ops.create_audit_entry
    fake_db.get_audit_entries = audit_ops.get_audit_entries
    fake_db.get_audit_entries_range = audit_ops.get_audit_entries_range
    fake_db_module.db = fake_db
    monkeypatch.setitem(sys.modules, "database", fake_db_module)

    # Stub models.User dependency too — service only reads .id and .email.
    if "models" not in sys.modules:
        fake_models = types.ModuleType("models")
        fake_models.User = type("User", (), {})
        monkeypatch.setitem(sys.modules, "models", fake_models)

    from services.platform_audit_service import (
        AuditActorType,
        AuditEventType,
        PlatformAuditService,
    )

    service = PlatformAuditService()
    return service, AuditEventType, AuditActorType


def test_service_logs_user_actor(audit_service, audit_ops):
    service, ETYPE, ATYPE = audit_service
    user = types.SimpleNamespace(id=42, email="alice@example.com")
    event_id = asyncio.run(
        service.log(
            event_type=ETYPE.AGENT_LIFECYCLE,
            event_action="create",
            source="api",
            actor_user=user,
            target_type="agent",
            target_id="oracle",
        )
    )
    assert event_id is not None
    row = audit_ops.get_audit_entry(event_id)
    assert row["actor_type"] == ATYPE.USER.value
    assert row["actor_id"] == "42"
    assert row["actor_email"] == "alice@example.com"
    assert row["target_id"] == "oracle"
    assert row["event_action"] == "create"


def test_service_logs_agent_actor(audit_service, audit_ops):
    service, ETYPE, ATYPE = audit_service
    event_id = asyncio.run(
        service.log(
            event_type=ETYPE.MCP_OPERATION,
            event_action="tool_call",
            source="mcp",
            actor_agent_name="oracle",
        )
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["actor_type"] == ATYPE.AGENT.value
    assert row["actor_id"] == "oracle"
    assert row["actor_email"] is None


def test_service_logs_mcp_client(audit_service, audit_ops):
    service, ETYPE, ATYPE = audit_service
    event_id = asyncio.run(
        service.log(
            event_type=ETYPE.MCP_OPERATION,
            event_action="tool_call",
            source="mcp",
            mcp_key_id="key-123",
            mcp_key_name="claude-code-dev",
            mcp_scope="user",
        )
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["actor_type"] == ATYPE.MCP_CLIENT.value
    assert row["actor_id"] == "key-123"
    assert row["mcp_key_name"] == "claude-code-dev"


def test_service_system_actor_when_mcp_scope_system(audit_service, audit_ops):
    service, ETYPE, ATYPE = audit_service
    event_id = asyncio.run(
        service.log(
            event_type=ETYPE.SYSTEM,
            event_action="startup",
            source="system",
            mcp_scope="system",
        )
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["actor_type"] == ATYPE.SYSTEM.value
    assert row["actor_id"] == "trinity-system"


def test_service_serializes_details_as_json(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = asyncio.run(
        service.log(
            event_type=ETYPE.CONFIGURATION,
            event_action="settings_change",
            source="api",
            actor_user=types.SimpleNamespace(id=1, email="a@b"),
            details={"key": "trinity_prompt", "old_len": 100, "new_len": 250},
        )
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["details"] == {"key": "trinity_prompt", "old_len": 100, "new_len": 250}


def test_service_never_raises_on_db_failure(audit_service, monkeypatch):
    """Audit failures must be logged but never propagated."""
    service, ETYPE, _ = audit_service

    def boom(_entry):
        raise RuntimeError("simulated DB failure")

    import database

    monkeypatch.setattr(database.db, "create_audit_entry", boom)

    # Must not raise.
    result = asyncio.run(
        service.log(
            event_type=ETYPE.AGENT_LIFECYCLE,
            event_action="create",
            source="api",
            actor_user=types.SimpleNamespace(id=1, email="a@b"),
        )
    )
    assert result is None


def test_service_generates_unique_event_ids(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    user = types.SimpleNamespace(id=1, email="a@b")
    ids = set()
    for _ in range(20):
        event_id = asyncio.run(
            service.log(
                event_type=ETYPE.AGENT_LIFECYCLE,
                event_action="create",
                source="api",
                actor_user=user,
            )
        )
        ids.add(event_id)
    assert len(ids) == 20  # all unique


def test_service_records_request_metadata(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    user = types.SimpleNamespace(id=1, email="a@b")
    event_id = asyncio.run(
        service.log(
            event_type=ETYPE.AGENT_LIFECYCLE,
            event_action="create",
            source="api",
            actor_user=user,
            actor_ip="10.0.0.1",
            request_id="req-xyz",
            endpoint="POST /api/agents",
        )
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["actor_ip"] == "10.0.0.1"
    assert row["request_id"] == "req-xyz"
    assert row["endpoint"] == "POST /api/agents"


# ---------------------------------------------------------------------------
# Agent lifecycle integration (SEC-001 Phase 2 — lifecycle subset)
# ---------------------------------------------------------------------------
#
# These tests exercise the exact shape the router handlers produce for
# create / start / stop / delete. Mirrors the kwargs used in
# routers/agents.py so a refactor of the call site cannot silently break
# the contract without breaking these tests.


def _lifecycle_log(service, etype, action, agent_name, **extra):
    """Fire the same service.log kwargs the router uses for one lifecycle event."""
    user = types.SimpleNamespace(id=7, email="owner@example.com")
    return asyncio.run(
        service.log(
            event_type=etype.AGENT_LIFECYCLE,
            event_action=action,
            source="api",
            actor_user=user,
            actor_ip="127.0.0.1",
            target_type="agent",
            target_id=agent_name,
            endpoint=f"/api/agents/{agent_name}",
            **extra,
        )
    )


def test_lifecycle_create_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _lifecycle_log(
        service,
        ETYPE,
        "create",
        "oracle",
        details={"template": "github:Org/repo", "base_image": "trinity-agent-base:latest"},
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_type"] == "agent_lifecycle"
    assert row["event_action"] == "create"
    assert row["target_type"] == "agent"
    assert row["target_id"] == "oracle"
    assert row["actor_type"] == "user"
    assert row["actor_email"] == "owner@example.com"
    assert row["details"]["template"] == "github:Org/repo"


def test_lifecycle_start_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _lifecycle_log(
        service, ETYPE, "start", "oracle", details={"credentials_injection": "success"}
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_action"] == "start"
    assert row["target_id"] == "oracle"
    assert row["details"]["credentials_injection"] == "success"


def test_lifecycle_stop_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _lifecycle_log(service, ETYPE, "stop", "oracle")
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_action"] == "stop"
    assert row["target_id"] == "oracle"
    assert row["details"] is None


def test_lifecycle_delete_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _lifecycle_log(service, ETYPE, "delete", "oracle")
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_action"] == "delete"
    assert row["target_id"] == "oracle"


def test_lifecycle_full_flow_creates_ordered_history(audit_service, audit_ops):
    """Create → start → stop → delete should produce 4 rows in temporal order."""
    service, ETYPE, _ = audit_service
    for action in ("create", "start", "stop", "delete"):
        _lifecycle_log(service, ETYPE, action, "ephemeral-1")

    rows = audit_ops.get_audit_entries(
        event_type="agent_lifecycle",
        target_type="agent",
        target_id="ephemeral-1",
    )
    # Newest first — reverse to get chronological order.
    actions = [r["event_action"] for r in reversed(rows)]
    assert actions == ["create", "start", "stop", "delete"]


# ---------------------------------------------------------------------------
# Phase 2b integration tests — auth, sharing, credentials, settings, rename
# ---------------------------------------------------------------------------


def _audit_log(service, etype, event_type, action, **extra):
    """Fire a service.log call matching Phase 2b router patterns."""
    user = types.SimpleNamespace(id=7, email="owner@example.com")
    return asyncio.run(
        service.log(
            event_type=event_type,
            event_action=action,
            source="api",
            actor_user=extra.pop("actor_user", user),
            actor_ip=extra.pop("actor_ip", "127.0.0.1"),
            target_type=extra.pop("target_type", None),
            target_id=extra.pop("target_id", None),
            endpoint=extra.pop("endpoint", "/api/test"),
            request_id=extra.pop("request_id", "req-001"),
            **extra,
        )
    )


# --- Authentication ---


def test_auth_login_success_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _audit_log(
        service, ETYPE, ETYPE.AUTHENTICATION, "login_success",
        target_type="user", target_id="admin",
        details={"method": "admin"},
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_type"] == "authentication"
    assert row["event_action"] == "login_success"
    assert row["target_id"] == "admin"
    assert row["details"]["method"] == "admin"


def test_auth_login_failed_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    # Failed login has no actor_user
    event_id = asyncio.run(
        service.log(
            event_type=ETYPE.AUTHENTICATION,
            event_action="login_failed",
            source="api",
            actor_ip="10.0.0.1",
            endpoint="/api/token",
            details={"method": "admin", "username": "admin"},
        )
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_type"] == "authentication"
    assert row["event_action"] == "login_failed"
    assert row["actor_type"] == "system"  # fallback when no actor
    assert row["actor_ip"] == "10.0.0.1"


def test_auth_email_login_success_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _audit_log(
        service, ETYPE, ETYPE.AUTHENTICATION, "login_success",
        target_type="user", target_id="alice",
        details={"method": "email", "email": "alice@example.com"},
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["details"]["method"] == "email"
    assert row["details"]["email"] == "alice@example.com"


# --- Authorization (sharing) ---


def test_share_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _audit_log(
        service, ETYPE, ETYPE.AUTHORIZATION, "share",
        target_type="agent", target_id="oracle",
        details={"shared_with": "bob@example.com"},
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_type"] == "authorization"
    assert row["event_action"] == "share"
    assert row["target_id"] == "oracle"
    assert row["details"]["shared_with"] == "bob@example.com"


def test_unshare_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _audit_log(
        service, ETYPE, ETYPE.AUTHORIZATION, "unshare",
        target_type="agent", target_id="oracle",
        details={"removed_email": "bob@example.com"},
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_action"] == "unshare"
    assert row["details"]["removed_email"] == "bob@example.com"


def test_access_request_approved_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _audit_log(
        service, ETYPE, ETYPE.AUTHORIZATION, "access_request_approved",
        target_type="agent", target_id="oracle",
        details={"email": "carol@example.com", "access_request_id": "ar-1"},
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_action"] == "access_request_approved"
    assert row["details"]["email"] == "carol@example.com"


def test_access_request_rejected_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _audit_log(
        service, ETYPE, ETYPE.AUTHORIZATION, "access_request_rejected",
        target_type="agent", target_id="oracle",
        details={"email": "carol@example.com", "access_request_id": "ar-2"},
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_action"] == "access_request_rejected"


# --- Credentials ---


def test_credential_inject_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _audit_log(
        service, ETYPE, ETYPE.CREDENTIALS, "inject",
        target_type="agent", target_id="oracle",
        details={"files": [".env", ".mcp.json"]},
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_type"] == "credentials"
    assert row["event_action"] == "inject"
    assert ".env" in row["details"]["files"]


def test_credential_export_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _audit_log(
        service, ETYPE, ETYPE.CREDENTIALS, "export",
        target_type="agent", target_id="oracle",
        details={"files_exported": 2},
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_action"] == "export"
    assert row["details"]["files_exported"] == 2


def test_credential_import_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _audit_log(
        service, ETYPE, ETYPE.CREDENTIALS, "import",
        target_type="agent", target_id="oracle",
        details={"files_imported": [".env"]},
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_action"] == "import"


# --- Configuration (settings) ---


def test_settings_change_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _audit_log(
        service, ETYPE, ETYPE.CONFIGURATION, "settings_change",
        details={"setting": "anthropic_api_key", "action": "update"},
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_type"] == "configuration"
    assert row["event_action"] == "settings_change"
    assert row["details"]["setting"] == "anthropic_api_key"


def test_settings_delete_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _audit_log(
        service, ETYPE, ETYPE.CONFIGURATION, "settings_change",
        details={"setting": "github_pat", "action": "delete"},
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["details"]["action"] == "delete"


# --- Agent rename ---


def test_rename_emits_row(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _audit_log(
        service, ETYPE, ETYPE.AGENT_LIFECYCLE, "rename",
        target_type="agent", target_id="oracle-v2",
        details={"old_name": "oracle", "new_name": "oracle-v2"},
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_type"] == "agent_lifecycle"
    assert row["event_action"] == "rename"
    assert row["details"]["old_name"] == "oracle"
    assert row["details"]["new_name"] == "oracle-v2"


# --- Request-ID propagation ---


def test_request_id_stored_in_entry(audit_service, audit_ops):
    service, ETYPE, _ = audit_service
    event_id = _audit_log(
        service, ETYPE, ETYPE.AUTHENTICATION, "login_success",
        request_id="req-abc-123",
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["request_id"] == "req-abc-123"


# --- Cross-category query ---


def test_phase2b_mixed_events_queryable(audit_service, audit_ops):
    """Multiple event types can be queried independently."""
    service, ETYPE, _ = audit_service
    _audit_log(service, ETYPE, ETYPE.AUTHENTICATION, "login_success")
    _audit_log(service, ETYPE, ETYPE.AUTHORIZATION, "share",
               target_type="agent", target_id="a")
    _audit_log(service, ETYPE, ETYPE.CREDENTIALS, "inject",
               target_type="agent", target_id="a")
    _audit_log(service, ETYPE, ETYPE.CONFIGURATION, "settings_change")
    _audit_log(service, ETYPE, ETYPE.AGENT_LIFECYCLE, "rename",
               target_type="agent", target_id="b")

    auth_rows = audit_ops.get_audit_entries(event_type="authentication")
    assert len(auth_rows) >= 1

    cred_rows = audit_ops.get_audit_entries(event_type="credentials")
    assert len(cred_rows) >= 1

    config_rows = audit_ops.get_audit_entries(event_type="configuration")
    assert len(config_rows) >= 1

    stats = audit_ops.get_audit_stats()
    assert stats["total"] >= 5


# ---------------------------------------------------------------------------
# Phase 3 — MCP tool call audit
# ---------------------------------------------------------------------------


def test_mcp_tool_call_emits_row(audit_service, audit_ops):
    """MCP tool calls produce rows with mcp_operation event type."""
    service, ETYPE, _ = audit_service
    event_id = asyncio.run(
        service.log(
            event_type=ETYPE.MCP_OPERATION,
            event_action="tool_call",
            source="mcp",
            mcp_key_id="key-42",
            mcp_key_name="dev-key",
            mcp_scope="user",
            details={"tool": "list_agents", "duration_ms": 150, "success": True},
        )
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["event_type"] == "mcp_operation"
    assert row["event_action"] == "tool_call"
    assert row["source"] == "mcp"
    assert row["mcp_key_id"] == "key-42"
    assert row["mcp_key_name"] == "dev-key"
    assert row["details"]["tool"] == "list_agents"
    assert row["details"]["success"] is True


def test_mcp_tool_call_agent_scope(audit_service, audit_ops):
    """Agent-scoped MCP tool calls record actor_agent_name."""
    service, ETYPE, _ = audit_service
    event_id = asyncio.run(
        service.log(
            event_type=ETYPE.MCP_OPERATION,
            event_action="tool_call",
            source="mcp",
            mcp_key_id="key-99",
            mcp_key_name="agent-key",
            mcp_scope="agent",
            actor_agent_name="research-bot",
            details={"tool": "chat_with_agent", "duration_ms": 5000, "success": True},
        )
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["actor_type"] == "agent"
    assert row["actor_id"] == "research-bot"
    assert row["mcp_scope"] == "agent"


def test_mcp_tool_call_failure(audit_service, audit_ops):
    """Failed MCP tool calls include error in details."""
    service, ETYPE, _ = audit_service
    event_id = asyncio.run(
        service.log(
            event_type=ETYPE.MCP_OPERATION,
            event_action="tool_call",
            source="mcp",
            mcp_key_id="key-1",
            mcp_key_name="test",
            mcp_scope="user",
            details={"tool": "delete_agent", "duration_ms": 30, "success": False, "error": "Not found"},
        )
    )
    row = audit_ops.get_audit_entry(event_id)
    assert row["details"]["success"] is False
    assert row["details"]["error"] == "Not found"


# ---------------------------------------------------------------------------
# Phase 4 — hash chain verification
# ---------------------------------------------------------------------------


def test_hash_chain_produces_hashes(audit_service, audit_ops):
    """With hash chain enabled, entries get entry_hash and previous_hash."""
    service, ETYPE, _ = audit_service
    service.enable_hash_chain(True)

    e1 = asyncio.run(
        service.log(
            event_type=ETYPE.SYSTEM,
            event_action="test_chain_1",
            source="test",
        )
    )
    e2 = asyncio.run(
        service.log(
            event_type=ETYPE.SYSTEM,
            event_action="test_chain_2",
            source="test",
        )
    )

    row1 = audit_ops.get_audit_entry(e1)
    row2 = audit_ops.get_audit_entry(e2)

    assert row1["entry_hash"] is not None
    assert len(row1["entry_hash"]) == 64  # SHA-256 hex
    assert row2["entry_hash"] is not None
    assert row2["previous_hash"] == row1["entry_hash"]

    # Disable for other tests
    service.enable_hash_chain(False)


def test_verify_chain_valid(audit_service, audit_ops):
    """verify_chain returns valid=True for intact chain."""
    service, ETYPE, _ = audit_service
    service.enable_hash_chain(True)

    asyncio.run(service.log(event_type=ETYPE.SYSTEM, event_action="v1", source="test"))
    asyncio.run(service.log(event_type=ETYPE.SYSTEM, event_action="v2", source="test"))
    asyncio.run(service.log(event_type=ETYPE.SYSTEM, event_action="v3", source="test"))

    # Get ID range
    entries = audit_ops.get_audit_entries(event_type="system", limit=100)
    ids = sorted(e["id"] for e in entries if e.get("entry_hash"))

    if len(ids) >= 2:
        result = asyncio.run(service.verify_chain(ids[0], ids[-1]))
        assert result["valid"] is True
        assert result["checked"] >= 2

    service.enable_hash_chain(False)


def test_verify_chain_empty_range(audit_service, audit_ops):
    """verify_chain on empty range returns valid=True, checked=0."""
    service, _, _ = audit_service
    result = asyncio.run(service.verify_chain(999999, 999999))
    assert result["valid"] is True
    assert result["checked"] == 0


# ---------------------------------------------------------------------------
# Phase 4 — export (db-layer only, no HTTP)
# ---------------------------------------------------------------------------


def test_export_entries_by_time_range(audit_service, audit_ops):
    """Entries in a time range are retrievable for export."""
    service, ETYPE, _ = audit_service
    asyncio.run(
        service.log(
            event_type=ETYPE.AGENT_LIFECYCLE,
            event_action="create",
            source="api",
            actor_user=types.SimpleNamespace(id=1, email="a@b"),
        )
    )
    # Fetch with wide time range
    entries = audit_ops.get_audit_entries(
        start_time="2020-01-01T00:00:00Z",
        end_time="2030-01-01T00:00:00Z",
        limit=100_000,
    )
    assert len(entries) >= 1


# ---------------------------------------------------------------------------
# Phase 5 regression — hash chain details round-trip
# ---------------------------------------------------------------------------


def test_compute_hash_details_string_dict_equivalence(audit_service):
    """Regression for Phase 5 bug.

    `details` is stored as a JSON string at write-time (``json.dumps``)
    and returned as a dict at read-time (``_row_to_dict``). Both forms
    must produce the same hash, otherwise ``verify_chain`` returns
    ``valid=False`` for every entry with non-null details.
    """
    service, _, _ = audit_service
    base = {
        "event_id": "fixed-uuid",
        "event_type": "system",
        "event_action": "test",
        "actor_id": "trinity-system",
        "target_id": None,
        "timestamp": "2026-04-21T00:00:00Z",
        "previous_hash": None,
    }
    details = {"key": "value", "n": 1, "nested": {"a": [1, 2, 3]}}
    write_form = {**base, "details": json.dumps(details)}
    read_form = {**base, "details": details}

    assert service._compute_hash(write_form) == service._compute_hash(read_form)


def test_verify_chain_valid_with_details(audit_service, audit_ops):
    """End-to-end: hash chain verifies across entries that carry details.

    Reproduces the exact scenario from the Phase 5 bug — entries with
    non-null details should round-trip through the DB and still verify.
    """
    service, ETYPE, _ = audit_service
    service.enable_hash_chain(True)
    try:
        asyncio.run(
            service.log(
                event_type=ETYPE.SYSTEM,
                event_action="with_details_1",
                source="test",
                details={"tool": "list_agents", "duration_ms": 12},
            )
        )
        asyncio.run(
            service.log(
                event_type=ETYPE.SYSTEM,
                event_action="with_details_2",
                source="test",
                details={"nested": {"ok": True, "items": [1, 2]}},
            )
        )

        entries = audit_ops.get_audit_entries(event_type="system", limit=100)
        ids = sorted(
            e["id"]
            for e in entries
            if e.get("entry_hash") and e["event_action"].startswith("with_details_")
        )
        assert len(ids) == 2

        result = asyncio.run(service.verify_chain(ids[0], ids[-1]))
        assert result["valid"] is True
        assert result["checked"] == 2
        assert result["first_invalid_id"] is None
    finally:
        service.enable_hash_chain(False)


# ---------------------------------------------------------------------------
# #905 — request_id correlation (MCP `mcp_operation` ⨝ backend `git_operation`)
# ---------------------------------------------------------------------------
#
# An MCP git tool stamps a per-call request id on its own `mcp_operation` audit
# row AND forwards it as X-Request-ID; the backend git handler adopts it and
# tags the `git_operation` row with the same id. Both rows are then retrievable
# together via the new `request_id` query filter. These tests pin that the two
# code paths (both funnel through `service.log`) produce joinable rows.


def test_905_request_id_correlation_returns_both_rows(audit_service, audit_ops):
    """The MCP row and the backend git row share one request_id and join."""
    service, ETYPE, _ = audit_service
    rid = "req-905-join"
    user = types.SimpleNamespace(id=7, email="owner@example.com")

    # 1) The MCP-side row (what routers/internal.py:log_audit_entry writes from
    #    an InternalAuditRequest forwarded by the MCP audit wrapper).
    asyncio.run(
        service.log(
            event_type=ETYPE.MCP_OPERATION,
            event_action="tool_call",
            source="mcp",
            mcp_key_id="key-1",
            mcp_scope="agent",
            actor_agent_name="alpha",
            target_type="agent",
            target_id="alpha",
            request_id=rid,
            details={"tool": "git_sync"},
        )
    )
    # 2) The backend-side row (what routers/git.py:_audit_git writes, adopting
    #    the X-Request-ID the MCP tool forwarded).
    asyncio.run(
        service.log(
            event_type=ETYPE.GIT_OPERATION,
            event_action="sync",
            source="api",
            actor_user=user,
            target_type="agent",
            target_id="alpha",
            request_id=rid,
            details={"success": True, "branch": "work"},
        )
    )

    joined = audit_ops.get_audit_entries(request_id=rid, limit=100)
    assert len(joined) == 2
    sources = {e["source"] for e in joined}
    assert sources == {"mcp", "api"}
    event_types = {e["event_type"] for e in joined}
    assert event_types == {"mcp_operation", "git_operation"}
    assert all(e["request_id"] == rid for e in joined)


def test_905_request_id_filter_isolates(audit_service, audit_ops):
    """The request_id filter returns only matching rows (no cross-talk)."""
    service, ETYPE, _ = audit_service
    asyncio.run(
        service.log(
            event_type=ETYPE.GIT_OPERATION,
            event_action="pull",
            source="api",
            actor_agent_name="alpha",
            request_id="req-A",
        )
    )
    asyncio.run(
        service.log(
            event_type=ETYPE.GIT_OPERATION,
            event_action="pull",
            source="api",
            actor_agent_name="alpha",
            request_id="req-B",
        )
    )
    only_a = audit_ops.get_audit_entries(request_id="req-A", limit=100)
    assert len(only_a) == 1
    assert only_a[0]["request_id"] == "req-A"
    assert audit_ops.count_audit_entries(request_id="req-A") == 1
    assert audit_ops.get_audit_entries(request_id="req-missing") == []


def test_905_request_id_failure_path_is_audited(audit_service, audit_ops):
    """A conflict (409) on a mutating git op still emits a joinable row (#905).

    Mirrors routers/git.py:_audit_git on the failure exit path — previously the
    conflict path emitted no row at all.
    """
    service, ETYPE, _ = audit_service
    rid = "req-905-conflict"
    asyncio.run(
        service.log(
            event_type=ETYPE.GIT_OPERATION,
            event_action="sync",
            source="api",
            actor_agent_name="alpha",
            target_type="agent",
            target_id="alpha",
            request_id=rid,
            details={"success": False, "status_code": 409, "conflict_type": "diverged_history"},
        )
    )
    rows = audit_ops.get_audit_entries(request_id=rid, limit=100)
    assert len(rows) == 1
    # The read layer deserializes the JSON `details` blob back to a dict.
    details = rows[0]["details"]
    if isinstance(details, str):
        details = json.loads(details)
    assert details["success"] is False
    assert details["conflict_type"] == "diverged_history"


def test_905_internal_audit_request_model_carries_request_id():
    """InternalAuditRequest exposes request_id so the MCP server can forward it (T2)."""
    import importlib

    models = importlib.import_module("models")
    req = models.InternalAuditRequest(
        event_type="mcp_operation",
        event_action="tool_call",
        source="mcp",
        request_id="req-xyz",
    )
    assert req.request_id == "req-xyz"
    # Default stays None so non-git tools (no requestId) are unaffected.
    assert (
        models.InternalAuditRequest(
            event_type="mcp_operation", event_action="tool_call"
        ).request_id
        is None
    )
