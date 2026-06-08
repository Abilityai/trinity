"""
Idempotency-key unit tests (RELIABILITY-006 / Issue #525).

Covers:
- IdempotencyOperations (db/idempotency.py): atomic claim, in-flight detection,
  completed replay, release, 24h expiry re-claim, purge, scope isolation.
- idempotency_service: key derivation (webhook body-hash, schedule), and the
  begin/complete/fail decision flow over a fake db delegating to the real ops.

Runs against an isolated temporary SQLite DB wired into db.connection via
monkeypatch — no live backend required. Mirrors test_audit_log_unit.py.
"""

import importlib.util
import os
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

# Add backend to path for direct imports.
_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "backend")
)
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)


# Override the backend-requiring autouse fixtures from the package conftest so
# these pure-unit tests run without a live backend (mirrors test_audit_log_unit).
@pytest.fixture(scope="session")
def api_client():
    yield None


@pytest.fixture(autouse=True)
def cleanup_after_test():
    yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def idem_ops(monkeypatch, tmp_path):
    """Fresh IdempotencyOperations bound to an isolated temp DB.

    The connection context commits on clean exit / rolls back on exception,
    exactly like the real db.connection.get_db_connection — required so a
    claim() committed by one call is visible to the next (cross-process
    atomicity is the whole point of the table).
    """
    db_path = str(tmp_path / "idem_test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE idempotency_keys (
            scope TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            execution_id TEXT,
            status TEXT NOT NULL,
            response_snapshot TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (scope, idempotency_key)
        )
        """
    )
    conn.commit()
    conn.close()

    class _ConnContext:
        def __enter__(self):
            self._conn = sqlite3.connect(db_path)
            self._conn.row_factory = sqlite3.Row
            return self._conn

        def __exit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
            self._conn.close()
            return False

    fake_conn_module = types.ModuleType("db.connection")
    fake_conn_module.get_db_connection = lambda: _ConnContext()
    fake_conn_module.DB_PATH = db_path
    monkeypatch.setitem(sys.modules, "db.connection", fake_conn_module)

    # Force reimport so the stub db.connection is picked up.
    monkeypatch.delitem(sys.modules, "db.idempotency", raising=False)
    from db.idempotency import IdempotencyOperations

    ops = IdempotencyOperations()
    # Pin the temp path so test helpers can open the SAME DB directly without
    # re-importing db.connection (which the autouse #762 baseline-restore may
    # reset to the real module mid-test).
    ops._test_db_path = db_path
    return ops


@pytest.fixture
def idem_service(idem_ops, monkeypatch):
    """Load services/idempotency_service.py standalone (bypassing the heavy
    services/__init__) with a fake `database.db` delegating to idem_ops."""
    fake_db = types.SimpleNamespace(
        idempotency_claim=idem_ops.claim,
        idempotency_attach_execution=idem_ops.attach_execution,
        idempotency_complete=idem_ops.complete,
        idempotency_release=idem_ops.release,
        idempotency_purge_expired=idem_ops.purge_expired,
    )
    fake_database = types.ModuleType("database")
    fake_database.db = fake_db
    monkeypatch.setitem(sys.modules, "database", fake_database)

    path = os.path.join(_backend_path, "services", "idempotency_service.py")
    spec = importlib.util.spec_from_file_location("_idem_service_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# IdempotencyOperations — claim lifecycle
# ---------------------------------------------------------------------------

class TestIdempotencyOps:
    def test_first_claim_is_new(self, idem_ops):
        res = idem_ops.claim("agent:a", "k1")
        assert res["state"] == "new"
        assert res["execution_id"] is None
        assert res["snapshot"] is None

    def test_duplicate_claim_is_in_flight(self, idem_ops):
        idem_ops.claim("agent:a", "k1")
        res = idem_ops.claim("agent:a", "k1")
        assert res["state"] == "in_flight"

    def test_completed_claim_replays_snapshot(self, idem_ops):
        idem_ops.claim("agent:a", "k1")
        idem_ops.complete("agent:a", "k1", "exec-123", {"response": "hi", "n": 1})
        res = idem_ops.claim("agent:a", "k1")
        assert res["state"] == "completed"
        assert res["execution_id"] == "exec-123"
        assert res["snapshot"] == {"response": "hi", "n": 1}

    def test_attach_execution_sets_id(self, idem_ops):
        idem_ops.claim("agent:a", "k1")
        idem_ops.attach_execution("agent:a", "k1", "exec-xyz")
        res = idem_ops.claim("agent:a", "k1")
        # still in_flight (not completed) but execution_id now populated
        assert res["state"] == "in_flight"
        assert res["execution_id"] == "exec-xyz"

    def test_release_allows_reclaim(self, idem_ops):
        idem_ops.claim("agent:a", "k1")
        idem_ops.release("agent:a", "k1")
        res = idem_ops.claim("agent:a", "k1")
        assert res["state"] == "new"

    def test_release_does_not_remove_completed(self, idem_ops):
        idem_ops.claim("agent:a", "k1")
        idem_ops.complete("agent:a", "k1", "exec-1", {"ok": True})
        idem_ops.release("agent:a", "k1")  # must be a no-op on completed rows
        res = idem_ops.claim("agent:a", "k1")
        assert res["state"] == "completed"

    def test_scope_isolation(self, idem_ops):
        idem_ops.claim("agent:a", "shared-key")
        # same key, different scope → independent claim
        res = idem_ops.claim("agent:b", "shared-key")
        assert res["state"] == "new"

    def test_expired_row_is_reclaimed_as_new(self, idem_ops):
        # Seed a row older than the TTL directly.
        old = _iso(datetime.now(timezone.utc) - timedelta(hours=25))
        with _direct(idem_ops) as conn:
            conn.execute(
                "INSERT INTO idempotency_keys (scope, idempotency_key, execution_id, "
                "status, response_snapshot, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("agent:a", "k1", "old-exec", "completed", '{"x":1}', old, old),
            )
            conn.commit()
        res = idem_ops.claim("agent:a", "k1", ttl_hours=24)
        assert res["state"] == "new"  # expired → re-claimed

    def test_purge_expired(self, idem_ops):
        old = _iso(datetime.now(timezone.utc) - timedelta(hours=30))
        with _direct(idem_ops) as conn:
            conn.execute(
                "INSERT INTO idempotency_keys (scope, idempotency_key, execution_id, "
                "status, response_snapshot, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("agent:a", "old", None, "completed", None, old, old),
            )
            conn.commit()
        idem_ops.claim("agent:a", "fresh")  # current
        removed = idem_ops.purge_expired(ttl_hours=24)
        assert removed == 1
        # fresh survives
        assert idem_ops.claim("agent:a", "fresh")["state"] == "in_flight"


import contextlib


@contextlib.contextmanager
def _direct(idem_ops):
    """Open a raw connection to the SAME temp DB the ops use, by path — avoids
    re-importing db.connection (which the autouse #762 restore can reset)."""
    conn = sqlite3.connect(idem_ops._test_db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# idempotency_service — derivation + decisions
# ---------------------------------------------------------------------------

class TestDerivation:
    def test_webhook_key_deterministic(self, idem_service):
        a = idem_service.derive_webhook_key("tok", b'{"x":1}')
        b = idem_service.derive_webhook_key("tok", b'{"x":1}')
        assert a == b
        assert a.startswith("auto:")

    def test_webhook_key_body_sensitive(self, idem_service):
        a = idem_service.derive_webhook_key("tok", b'{"x":1}')
        b = idem_service.derive_webhook_key("tok", b'{"x":2}')
        assert a != b

    def test_webhook_key_token_sensitive(self, idem_service):
        a = idem_service.derive_webhook_key("tok1", b"body")
        b = idem_service.derive_webhook_key("tok2", b"body")
        assert a != b

    def test_schedule_key(self, idem_service):
        assert idem_service.derive_schedule_key("exec-9") == "sched:exec-9"

    def test_scope_helpers(self, idem_service):
        assert idem_service.make_agent_scope("bob") == "agent:bob"
        assert idem_service.make_webhook_scope("zzz") == "webhook:zzz"


class TestServiceDecisions:
    def test_no_key_is_disabled_noop(self, idem_service):
        d = idem_service.begin("agent:a", None)
        assert d.enabled is False
        assert d.replay is False
        # complete/fail are safe no-ops on a disabled decision
        idem_service.complete(d, "exec", {"a": 1})
        idem_service.fail(d)

    def test_first_begin_then_replay_completed(self, idem_service):
        d1 = idem_service.begin("agent:a", "k1")
        assert d1.enabled and not d1.replay
        idem_service.complete(d1, "exec-1", {"response": "done"})

        d2 = idem_service.begin("agent:a", "k1")
        assert d2.replay is True
        assert d2.in_flight is False
        assert d2.execution_id == "exec-1"
        assert d2.snapshot == {"response": "done"}

    def test_begin_in_flight_replay(self, idem_service):
        idem_service.begin("agent:a", "k1")  # leaves in_flight
        d2 = idem_service.begin("agent:a", "k1")
        assert d2.replay is True
        assert d2.in_flight is True

    def test_fail_releases_claim_for_retry(self, idem_service):
        d1 = idem_service.begin("agent:a", "k1")
        idem_service.fail(d1)
        d2 = idem_service.begin("agent:a", "k1")
        assert d2.replay is False  # reclaimed as new
