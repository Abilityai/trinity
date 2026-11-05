"""
Agents wake on an answered ask by default (abilityai/trinity-enterprise#1108).
Related flow: docs/memory/feature-flows/operating-room.md (the wake)

The per-agent "wake this agent when its asks end" flag (ent#329) was opt-in:
``agent_ownership.operator_resume_enabled INTEGER DEFAULT 0``, read as False
when NULL. The operator's ruling after the 2026-11-05 film: on by default,
on every instance, with the owner's OFF still honoured.

The column becomes tri-state going forward — NULL ⇒ the platform default
(ON), 0 ⇒ the owner's explicit OFF, 1 ⇒ the owner's explicit ON — so the
flag can still say which rows an owner touched. Every toggle is audited
(`operator_resume_config`, `details.enabled`), so the one-shot migration
moves the never-toggled 0s to NULL and leaves an agent whose LATEST toggle
was OFF at 0.

Shapes proven here, each at the layer the change lives in:
* the getter (the one authority every wake reads through);
* the owner-row insert, on an UPGRADED instance's DDL (baked ``DEFAULT 0``);
* both migration tracks, over the real DDL, idempotent, and registered;
* the GET route's honest status, through the real handler.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_backend = str(Path(__file__).resolve().parents[2] / "src" / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

pytestmark = pytest.mark.unit

_REVISION = "0116_operator_resume_default_on"
_PARENT = "0115_access_requests_requester_identity"


# ---------------------------------------------------------------------------
# A file-backed SQLite the mixin's engine and the seeding connection share
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db_conn(tmp_path, monkeypatch):
    """The ent#329 harness, on the UPGRADED-instance DDL: a column whose baked
    ``DEFAULT 0`` SQLite keeps forever (an ALTER cannot change it)."""
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE agent_ownership (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            is_system INTEGER DEFAULT 0,
            execution_timeout_seconds INTEGER DEFAULT 900,
            require_email INTEGER DEFAULT 0,
            is_ephemeral INTEGER DEFAULT 0,
            ephemeral_max_executions INTEGER,
            ephemeral_expires_at TEXT,
            spawned_by_agent TEXT,
            spawned_by_key_id TEXT,
            max_parallel_tasks INTEGER,
            operator_resume_enabled INTEGER DEFAULT 0,
            deleted_at TEXT
        )
        """
    )
    conn.commit()

    import db.engine as engine_mod
    engine_mod.dispose_engines()
    yield conn
    engine_mod.dispose_engines()
    conn.close()


@pytest.fixture
def mixin(tmp_db_conn):
    try:
        from db.agent_settings.operator_resume import OperatorResumeMixin
    except ImportError:
        pytest.skip("backend venv required (no `db.agent_settings` import)")

    class _Ops(OperatorResumeMixin):
        pass

    return _Ops()


def _seed(conn, name: str, *, enabled, deleted_at=None):
    conn.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at,"
        " operator_resume_enabled, deleted_at) VALUES (?, 1, '2026-01-01T00:00:00Z', ?, ?)",
        (name, enabled, deleted_at),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# The getter — the one authority every wake reads through
# ---------------------------------------------------------------------------

class TestTheDefaultIsOn:
    def test_an_untouched_row_wakes(self, mixin, tmp_db_conn):
        """NULL is "the owner never said" — and the platform's answer is ON."""
        _seed(tmp_db_conn, "untouched", enabled=None)
        assert mixin.get_operator_resume_enabled("untouched") is True

    def test_an_explicit_off_is_honoured(self, mixin, tmp_db_conn):
        _seed(tmp_db_conn, "owner-off", enabled=0)
        assert mixin.get_operator_resume_enabled("owner-off") is False

    def test_an_explicit_on_still_reads_on(self, mixin, tmp_db_conn):
        _seed(tmp_db_conn, "owner-on", enabled=1)
        assert mixin.get_operator_resume_enabled("owner-on") is True

    def test_an_unknown_agent_never_wakes(self, mixin):
        """No row is not "the default": a wake for a name nobody owns is spend
        on nothing. Unchanged from ent#329."""
        assert mixin.get_operator_resume_enabled("no-such-agent") is False

    def test_a_soft_deleted_agent_never_wakes(self, mixin, tmp_db_conn):
        _seed(tmp_db_conn, "gone", enabled=None, deleted_at="2026-08-01T00:00:00Z")
        assert mixin.get_operator_resume_enabled("gone") is False

    def test_the_owner_can_switch_it_off_and_back_on(self, mixin, tmp_db_conn):
        _seed(tmp_db_conn, "toggled", enabled=None)
        assert mixin.set_operator_resume_enabled("toggled", False) is True
        assert mixin.get_operator_resume_enabled("toggled") is False
        assert mixin.set_operator_resume_enabled("toggled", True) is True
        assert mixin.get_operator_resume_enabled("toggled") is True


# ---------------------------------------------------------------------------
# A new agent on an UPGRADED instance (AC #1, first half)
# ---------------------------------------------------------------------------

def test_a_new_agent_wakes_even_where_the_column_still_bakes_default_zero(mixin, tmp_db_conn):
    """`register_agent_owner` must pass the column explicitly: an upgraded
    SQLite instance keeps the ent#329 ``DEFAULT 0`` on the column (the #665 /
    #1129 lesson), and an omitted column would make every NEW agent an
    explicit OFF — the opposite of the ruling."""
    try:
        from db.agents import AgentOperations
    except ImportError:
        pytest.skip("backend venv required")

    ops = AgentOperations(SimpleNamespace(get_user_by_username=lambda u: {"id": 1}))
    assert ops.register_agent_owner("fresh-agent", "owner") is True
    stored = tmp_db_conn.execute(
        "SELECT operator_resume_enabled FROM agent_ownership WHERE agent_name='fresh-agent'"
    ).fetchone()[0]
    assert stored is None, "the insert let SQLite's baked DEFAULT 0 stamp an explicit OFF"
    assert mixin.get_operator_resume_enabled("fresh-agent") is True


# ---------------------------------------------------------------------------
# The one-shot migration — both tracks (AC #1, second half)
# ---------------------------------------------------------------------------

@pytest.fixture()
def backfill_db():
    from db.schema import TABLES
    conn = sqlite3.connect(":memory:")
    for t in ("agent_ownership", "audit_log"):
        conn.execute(TABLES[t])
    now = "2026-11-01T00:00:00.000000Z"

    def agent(name, *, flag, deleted=None):
        conn.execute(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at,"
            " operator_resume_enabled) VALUES (?,1,?,?,?)", (name, now, deleted, flag))

    def toggled(name, enabled, at):
        conn.execute(
            "INSERT INTO audit_log (event_id, event_type, event_action, actor_type, target_type,"
            " target_id, timestamp, details, source, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"ev-{name}-{at}", "configuration", "operator_resume_config", "user", "agent",
             name, at, json.dumps({"enabled": enabled}), "api", at))

    agent("legacy-zero", flag=0)                       # never toggled → NULL (default ON)
    agent("legacy-null", flag=None)                    # untouched → stays NULL
    agent("owner-on", flag=1)                          # explicit ON → stays 1
    toggled("owner-on", True, "2026-10-01T00:00:00Z")
    agent("owner-off", flag=0)                         # explicit OFF → stays 0
    toggled("owner-off", False, "2026-10-01T00:00:00Z")
    agent("on-then-off", flag=0)                       # latest toggle OFF → stays 0
    toggled("on-then-off", True, "2026-10-01T00:00:00Z")
    toggled("on-then-off", False, "2026-10-02T00:00:00Z")
    agent("off-then-on", flag=1)                       # latest toggle ON → stays 1
    toggled("off-then-on", False, "2026-10-01T00:00:00Z")
    toggled("off-then-on", True, "2026-10-02T00:00:00Z")
    agent("deleted-zero", flag=0, deleted=now)         # a recovered agent follows the default
    # A different agent's OFF must not shadow this one (the subquery is per target).
    agent("other-config-event", flag=0)
    conn.execute(
        "INSERT INTO audit_log (event_id, event_type, event_action, actor_type, target_type,"
        " target_id, timestamp, details, source, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("ev-other", "configuration", "consent_config", "user", "agent", "other-config-event",
         "2026-10-01T00:00:00Z", json.dumps({"enabled": False}), "api", "2026-10-01T00:00:00Z"))
    conn.commit()
    yield conn
    conn.close()


EXPECTED = {
    "legacy-zero": None, "legacy-null": None, "owner-on": 1, "owner-off": 0,
    "on-then-off": 0, "off-then-on": 1, "deleted-zero": None, "other-config-event": None,
}


def _flags(conn):
    return dict(conn.execute("SELECT agent_name, operator_resume_enabled FROM agent_ownership"))


def test_sqlite_migration_moves_untoggled_zeros_to_the_default(backfill_db):
    from db.migrations import _migrate_operator_resume_default_on
    _migrate_operator_resume_default_on(backfill_db.cursor(), backfill_db)
    assert _flags(backfill_db) == EXPECTED
    _migrate_operator_resume_default_on(backfill_db.cursor(), backfill_db)  # idempotent
    assert _flags(backfill_db) == EXPECTED


def test_sqlite_migration_without_an_audit_table_treats_every_zero_as_untoggled():
    """A DB old enough to have no audit_log has no explicit-OFF signal at all."""
    from db.schema import TABLES
    from db.migrations import _migrate_operator_resume_default_on
    conn = sqlite3.connect(":memory:")
    conn.execute(TABLES["agent_ownership"])
    conn.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at, operator_resume_enabled)"
        " VALUES ('a', 1, '2026-01-01T00:00:00Z', 0)")
    conn.commit()
    _migrate_operator_resume_default_on(conn.cursor(), conn)
    assert _flags(conn) == {"a": None}


def test_alembic_revision_matches_the_sqlite_track(backfill_db, monkeypatch):
    import sqlalchemy as sa
    path = Path(__file__).resolve().parents[2] / "src/backend/migrations/versions" / f"{_REVISION}.py"
    spec = importlib.util.spec_from_file_location("rev0116_ent1108", path)
    rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev)
    assert (rev.revision, rev.down_revision) == (_REVISION, _PARENT)

    engine = sa.create_engine("sqlite://", creator=lambda: backfill_db)
    with engine.begin() as conn:
        monkeypatch.setattr(rev.op, "get_bind", lambda: conn)
        monkeypatch.setattr(rev.op, "alter_column", lambda *a, **k: None)  # SQLite cannot; PG does
        rev.upgrade()
        rev.upgrade()
    assert _flags(backfill_db) == EXPECTED


def test_the_migration_is_registered_on_the_sqlite_track():
    from db.migrations import MIGRATIONS
    names = [name for name, _ in MIGRATIONS]
    assert "operator_resume_default_on" in names
    assert names.index("operator_resume_default_on") > names.index("agent_ownership_operator_resume")


def test_the_fresh_ddl_no_longer_bakes_an_explicit_off():
    """A fresh install's column must not carry ``DEFAULT 0``: a writer that
    omits the column would otherwise stamp the owner's OFF on every agent."""
    from db.schema import TABLES
    conn = sqlite3.connect(":memory:")
    conn.execute(TABLES["agent_ownership"])
    default = next(
        row[4] for row in conn.execute("PRAGMA table_info(agent_ownership)")
        if row[1] == "operator_resume_enabled"
    )
    assert default is None


# ---------------------------------------------------------------------------
# The Settings read: honest status through the real handler (AC #2)
# ---------------------------------------------------------------------------

class TestTheSettingsReadIsHonest:
    @pytest.fixture
    def router(self, mixin, monkeypatch):
        try:
            from routers import agents as agents_router
        except Exception as e:  # pragma: no cover — venv-less checkout
            pytest.skip(f"backend venv required: {e}")
        monkeypatch.setattr(agents_router, "db", mixin, raising=False)
        return agents_router

    def _read(self, router, name):
        from models import User
        person = User(id=1, username="owner", role="admin")
        return asyncio.run(router.get_operator_resume_endpoint(agent_name=name, current_user=person))

    def test_an_untouched_agent_reads_on(self, router, tmp_db_conn):
        _seed(tmp_db_conn, "untouched", enabled=None)
        assert self._read(router, "untouched") == {"agent_name": "untouched", "enabled": True}

    def test_an_owner_off_reads_off(self, router, tmp_db_conn):
        _seed(tmp_db_conn, "owner-off", enabled=0)
        assert self._read(router, "owner-off") == {"agent_name": "owner-off", "enabled": False}
