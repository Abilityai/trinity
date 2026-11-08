"""
db.transfer_agent_ownership — the OSS ownership-transfer primitive
(abilityai/trinity-enterprise#1147, ruling T3).

A hub-pool spare spawned by an agent is owned by the spawner's owner and
carries spawn provenance in four OSS tables. Adoption re-owns it to the
steward in ONE transaction, guarded by a CAS on the ownership row:

* ``agent_ownership``: owner, spawn columns cleared, not ephemeral, autonomy on;
* ``agent_schedules.owner_id`` and ``agent_reminders.owner_id`` follow the owner;
* the ``spawn:<spawner>`` edge in ``agent_permissions`` is removed.

A CAS miss returns False and writes nothing. MCP keys are deliberately NOT
touched here (the enterprise caller deactivates them).

Driven at the layer the change lives in (the AgentOperations mixin) over the
real ``db/schema.py`` DDL, and through the ``DatabaseManager`` facade, which
delegates by name — a method missing there AttributeErrors in production.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_backend = str(Path(__file__).resolve().parents[2] / "src" / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

pytestmark = pytest.mark.unit

SPAWNER = "trinity-ops"
SPARE = "spare-1"
OWNER_A = 1
OWNER_B = 7
_NOW = "2026-11-08T00:00:00Z"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    from db.schema import create_all_tables

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    create_all_tables(c.cursor())
    c.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at, is_ephemeral,"
        " spawned_by_agent, spawned_by_key_id, autonomy_enabled)"
        " VALUES (?, ?, ?, 1, ?, 'key-1', 0)",
        (SPARE, OWNER_A, _NOW, SPAWNER),
    )
    c.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at)"
        " VALUES ('bystander', ?, ?)",
        (OWNER_A, _NOW),
    )
    for agent in (SPARE, "bystander"):
        c.execute(
            "INSERT INTO agent_schedules (id, agent_name, name, cron_expression, message,"
            " owner_id, created_at, updated_at) VALUES (?, ?, 'n', '* * * * *', 'm', ?, ?, ?)",
            (f"s-{agent}", agent, OWNER_A, _NOW, _NOW),
        )
        c.execute(
            "INSERT INTO agent_reminders (id, agent_name, message, fire_at, owner_id, created_at)"
            " VALUES (?, ?, 'm', ?, ?, ?)",
            (f"r-{agent}", agent, _NOW, OWNER_A, _NOW),
        )
    c.execute(
        "INSERT INTO agent_permissions (source_agent, target_agent, created_by) VALUES (?, ?, ?)",
        (SPAWNER, SPARE, f"spawn:{SPAWNER}"),
    )
    # An edge that is not the spawn edge must survive.
    c.execute(
        "INSERT INTO agent_permissions (source_agent, target_agent, created_by) VALUES (?, ?, 'u1')",
        ("other", SPARE),
    )
    c.commit()

    import db.engine as engine_mod
    engine_mod.dispose_engines()
    yield c
    engine_mod.dispose_engines()
    c.close()


@pytest.fixture
def ops():
    from db.agent_settings.autonomy import AutonomyMixin
    return AutonomyMixin()


def _found():
    return {"owner_id": OWNER_A, "spawned_by_agent": SPAWNER, "spawned_by_key_id": "key-1"}


def _row(c, agent=SPARE):
    return c.execute("SELECT * FROM agent_ownership WHERE agent_name = ?", (agent,)).fetchone()


def _owners(c, table, agent):
    return [r[0] for r in c.execute(f"SELECT owner_id FROM {table} WHERE agent_name = ?", (agent,))]


def _edges(c):
    return {
        (r["source_agent"], r["created_by"])
        for r in c.execute("SELECT * FROM agent_permissions WHERE target_agent = ?", (SPARE,))
    }


class TestTransferAgentOwnership:
    def test_transfer_agent_ownership_rewrites_every_owner_carrying_row(self, conn, ops):
        assert ops.transfer_agent_ownership(SPARE, to_user_id=OWNER_B, expect=_found()) is True

        row = _row(conn)
        assert row["owner_id"] == OWNER_B
        assert row["spawned_by_agent"] is None
        assert row["spawned_by_key_id"] is None
        assert row["is_ephemeral"] == 0
        assert row["autonomy_enabled"] == 1
        assert _owners(conn, "agent_schedules", SPARE) == [OWNER_B]
        assert _owners(conn, "agent_reminders", SPARE) == [OWNER_B]
        assert _edges(conn) == {("other", "u1")}

        # Another agent of the same owner is untouched.
        assert _row(conn, "bystander")["owner_id"] == OWNER_A
        assert _owners(conn, "agent_schedules", "bystander") == [OWNER_A]
        assert _owners(conn, "agent_reminders", "bystander") == [OWNER_A]

    @pytest.mark.parametrize(
        "stale",
        [
            {"owner_id": 99},
            {"spawned_by_agent": "someone-else"},
            {"spawned_by_key_id": None},
            {"autonomy_enabled": 1},
        ],
    )
    def test_transfer_agent_ownership_cas_miss_returns_false_and_writes_nothing(
        self, conn, ops, stale
    ):
        expect = {**_found(), **stale}
        assert ops.transfer_agent_ownership(SPARE, to_user_id=OWNER_B, expect=expect) is False

        row = _row(conn)
        assert row["owner_id"] == OWNER_A
        assert row["spawned_by_agent"] == SPAWNER
        assert row["spawned_by_key_id"] == "key-1"
        assert row["is_ephemeral"] == 1
        assert row["autonomy_enabled"] == 0
        assert _owners(conn, "agent_schedules", SPARE) == [OWNER_A]
        assert _owners(conn, "agent_reminders", SPARE) == [OWNER_A]
        assert _edges(conn) == {(SPAWNER, f"spawn:{SPAWNER}"), ("other", "u1")}

    def test_transfer_agent_ownership_null_expectation_matches_null_column(self, conn, ops):
        conn.execute(
            "UPDATE agent_ownership SET spawned_by_agent = NULL, spawned_by_key_id = NULL"
            " WHERE agent_name = ?",
            (SPARE,),
        )
        conn.commit()
        expect = {"owner_id": OWNER_A, "spawned_by_agent": None, "spawned_by_key_id": None}
        assert ops.transfer_agent_ownership(SPARE, to_user_id=OWNER_B, expect=expect) is True
        assert _row(conn)["owner_id"] == OWNER_B
        # No spawner on the row → no spawn edge to remove; the planted one stays.
        assert (SPAWNER, f"spawn:{SPAWNER}") in _edges(conn)

    def test_transfer_agent_ownership_deleted_or_missing_agent_is_a_miss(self, conn, ops):
        assert ops.transfer_agent_ownership("nope", to_user_id=OWNER_B, expect={}) is False
        conn.execute("UPDATE agent_ownership SET deleted_at = ? WHERE agent_name = ?", (_NOW, SPARE))
        conn.commit()
        assert ops.transfer_agent_ownership(SPARE, to_user_id=OWNER_B, expect=_found()) is False
        assert _owners(conn, "agent_schedules", SPARE) == [OWNER_A]

    def test_transfer_agent_ownership_rejects_unknown_expect_column(self, conn, ops):
        with pytest.raises(ValueError, match="no_such_col"):
            ops.transfer_agent_ownership(
                SPARE, to_user_id=OWNER_B, expect={"no_such_col": 1}
            )
        assert _row(conn)["owner_id"] == OWNER_A

    def test_transfer_agent_ownership_facade_delegates(self, conn):
        from database import DatabaseManager

        manager = DatabaseManager.__new__(DatabaseManager)
        from db.agent_settings.autonomy import AutonomyMixin
        manager._agent_ops = AutonomyMixin()
        assert manager.transfer_agent_ownership(
            SPARE, to_user_id=OWNER_B, expect=_found()
        ) is True
        assert _row(conn)["owner_id"] == OWNER_B
