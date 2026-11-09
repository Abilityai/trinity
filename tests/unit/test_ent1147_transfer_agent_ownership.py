"""
db.transfer_agent_ownership — the OSS ownership-transfer primitive
(abilityai/trinity-enterprise#1147).

An agent spawned by another agent is owned by the spawner's owner and carries
spawn provenance. The transfer re-owns it to another user in ONE transaction,
guarded by a CAS on the live ``agent_ownership`` row:

* every column in ``TRANSFER_OWNER_COLUMNS`` takes the new owner
  (``agent_ownership``, ``agent_schedules``, ``agent_reminders``);
* everything else is a named, required option — clear the spawn provenance
  (and the ``spawn:<spawner>`` edge), clear ``is_ephemeral``, turn autonomy
  on, or write a captured spawn provenance back (the inverse transfer).

A CAS miss returns False and writes nothing. MCP keys are deliberately NOT
touched here; the caller handles them.

Driven at the layer the change lives in (the AgentOperations mixin) over the
real ``db/schema.py`` DDL, and through the ``DatabaseManager`` facade, which
delegates by name — a method missing there AttributeErrors in production. The
registry guard at the bottom fails when a table keyed by ``agent_name`` gains
an owner-carrying column the transfer does not write.
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

SPAWNER = "parent-agent"
CHILD = "child-1"
OWNER_A = 1
OWNER_B = 7
_NOW = "2026-11-08T00:00:00Z"

_ALL_ON = dict(clear_spawn_provenance=True, clear_ephemeral=True, enable_autonomy=True)
_ALL_OFF = dict(clear_spawn_provenance=False, clear_ephemeral=False, enable_autonomy=False)


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
        (CHILD, OWNER_A, _NOW, SPAWNER),
    )
    c.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at)"
        " VALUES ('bystander', ?, ?)",
        (OWNER_A, _NOW),
    )
    for agent in (CHILD, "bystander"):
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
        (SPAWNER, CHILD, f"spawn:{SPAWNER}"),
    )
    # An edge that is not the spawn edge must survive.
    c.execute(
        "INSERT INTO agent_permissions (source_agent, target_agent, created_by) VALUES (?, ?, 'u1')",
        ("other", CHILD),
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


def _row(c, agent=CHILD):
    return c.execute("SELECT * FROM agent_ownership WHERE agent_name = ?", (agent,)).fetchone()


def _owners(c, table, agent):
    return [r[0] for r in c.execute(f"SELECT owner_id FROM {table} WHERE agent_name = ?", (agent,))]


def _edges(c):
    return {
        (r["source_agent"], r["created_by"])
        for r in c.execute("SELECT * FROM agent_permissions WHERE target_agent = ?", (CHILD,))
    }


class TestTransferAgentOwnership:
    def test_transfer_agent_ownership_rewrites_every_owner_carrying_row(self, conn, ops):
        assert ops.transfer_agent_ownership(CHILD, to_user_id=OWNER_B, expect=_found(), **_ALL_ON) is True

        row = _row(conn)
        assert row["owner_id"] == OWNER_B
        assert row["spawned_by_agent"] is None
        assert row["spawned_by_key_id"] is None
        assert row["is_ephemeral"] == 0
        assert row["autonomy_enabled"] == 1
        assert _owners(conn, "agent_schedules", CHILD) == [OWNER_B]
        assert _owners(conn, "agent_reminders", CHILD) == [OWNER_B]
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
        assert ops.transfer_agent_ownership(CHILD, to_user_id=OWNER_B, expect=expect, **_ALL_ON) is False

        row = _row(conn)
        assert row["owner_id"] == OWNER_A
        assert row["spawned_by_agent"] == SPAWNER
        assert row["spawned_by_key_id"] == "key-1"
        assert row["is_ephemeral"] == 1
        assert row["autonomy_enabled"] == 0
        assert _owners(conn, "agent_schedules", CHILD) == [OWNER_A]
        assert _owners(conn, "agent_reminders", CHILD) == [OWNER_A]
        assert _edges(conn) == {(SPAWNER, f"spawn:{SPAWNER}"), ("other", "u1")}

    def test_transfer_agent_ownership_null_expectation_matches_null_column(self, conn, ops):
        conn.execute(
            "UPDATE agent_ownership SET spawned_by_agent = NULL, spawned_by_key_id = NULL"
            " WHERE agent_name = ?",
            (CHILD,),
        )
        conn.commit()
        expect = {"owner_id": OWNER_A, "spawned_by_agent": None, "spawned_by_key_id": None}
        assert ops.transfer_agent_ownership(CHILD, to_user_id=OWNER_B, expect=expect, **_ALL_ON) is True
        assert _row(conn)["owner_id"] == OWNER_B
        # No spawner on the row → no spawn edge to remove; the planted one stays.
        assert (SPAWNER, f"spawn:{SPAWNER}") in _edges(conn)

    def test_transfer_agent_ownership_deleted_or_missing_agent_is_a_miss(self, conn, ops):
        assert ops.transfer_agent_ownership("nope", to_user_id=OWNER_B, expect={}, **_ALL_ON) is False
        conn.execute("UPDATE agent_ownership SET deleted_at = ? WHERE agent_name = ?", (_NOW, CHILD))
        conn.commit()
        assert ops.transfer_agent_ownership(CHILD, to_user_id=OWNER_B, expect=_found(), **_ALL_ON) is False
        assert _owners(conn, "agent_schedules", CHILD) == [OWNER_A]

    def test_transfer_agent_ownership_rejects_unknown_expect_column(self, conn, ops):
        with pytest.raises(ValueError, match="no_such_col"):
            ops.transfer_agent_ownership(
                CHILD, to_user_id=OWNER_B, expect={"no_such_col": 1}, **_ALL_ON
            )
        assert _row(conn)["owner_id"] == OWNER_A

    def test_transfer_agent_ownership_facade_delegates(self, conn):
        from database import DatabaseManager

        manager = DatabaseManager.__new__(DatabaseManager)
        from db.agent_settings.autonomy import AutonomyMixin
        manager._agent_ops = AutonomyMixin()
        assert manager.transfer_agent_ownership(
            CHILD, to_user_id=OWNER_B, expect=_found(), **_ALL_ON
        ) is True
        assert _row(conn)["owner_id"] == OWNER_B

    def test_options_off_moves_only_the_owner_columns(self, conn, ops):
        """No option → nothing about the agent changes but who owns it."""
        assert ops.transfer_agent_ownership(
            CHILD, to_user_id=OWNER_B, expect=_found(), **_ALL_OFF
        ) is True

        row = _row(conn)
        assert row["owner_id"] == OWNER_B
        assert row["spawned_by_agent"] == SPAWNER
        assert row["spawned_by_key_id"] == "key-1"
        assert row["is_ephemeral"] == 1
        assert row["autonomy_enabled"] == 0
        assert _owners(conn, "agent_schedules", CHILD) == [OWNER_B]
        assert _owners(conn, "agent_reminders", CHILD) == [OWNER_B]
        assert _edges(conn) == {(SPAWNER, f"spawn:{SPAWNER}"), ("other", "u1")}

    @pytest.mark.parametrize("option", sorted(_ALL_ON))
    def test_every_side_effect_option_is_required(self, conn, ops, option):
        kwargs = {k: v for k, v in _ALL_ON.items() if k != option}
        with pytest.raises(TypeError, match=option):
            ops.transfer_agent_ownership(CHILD, to_user_id=OWNER_B, expect=_found(), **kwargs)
        assert _row(conn)["owner_id"] == OWNER_A

    def test_the_inverse_transfer_puts_the_found_row_back(self, conn, ops):
        assert ops.transfer_agent_ownership(
            CHILD, to_user_id=OWNER_B, expect=_found(), **_ALL_ON
        ) is True
        transferred = {"owner_id": OWNER_B, "spawned_by_agent": None, "spawned_by_key_id": None}
        assert ops.transfer_agent_ownership(
            CHILD, to_user_id=OWNER_A, expect=transferred, **_ALL_OFF,
            restore_spawn_provenance={"spawned_by_agent": SPAWNER, "spawned_by_key_id": "key-1"},
        ) is True

        row = _row(conn)
        assert row["owner_id"] == OWNER_A
        assert row["spawned_by_agent"] == SPAWNER
        assert row["spawned_by_key_id"] == "key-1"
        assert _owners(conn, "agent_schedules", CHILD) == [OWNER_A]
        assert _owners(conn, "agent_reminders", CHILD) == [OWNER_A]

    def test_restore_and_clear_spawn_provenance_are_exclusive(self, conn, ops):
        with pytest.raises(ValueError, match="exclusive"):
            ops.transfer_agent_ownership(
                CHILD, to_user_id=OWNER_B, expect=_found(), **_ALL_ON,
                restore_spawn_provenance={"spawned_by_agent": SPAWNER},
            )
        with pytest.raises(ValueError, match="owner_id"):
            ops.transfer_agent_ownership(
                CHILD, to_user_id=OWNER_B, expect=_found(), **_ALL_OFF,
                restore_spawn_provenance={"owner_id": 3},
            )
        assert _row(conn)["owner_id"] == OWNER_A


# ---------------------------------------------------------------------------
# Registry guard: the transfer's write-set covers every owner-carrying column
# ---------------------------------------------------------------------------

def _owner_carrying_columns_in_metadata():
    from db.tables import metadata

    return {
        (table.name, column)
        for table in metadata.sorted_tables
        if "agent_name" in table.c
        for column in table.c.keys()
        if "owner" in column
    }


def _owner_carrying_columns_in_sqlite_ddl(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    found = set()
    for table in tables:
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if "agent_name" in columns:
            found |= {(table, c) for c in columns if "owner" in c}
    return found


class TestTransferOwnerColumnRegistry:
    """A table keyed by ``agent_name`` that names an owner must be in the
    transfer's write-set, in BOTH schema tracks; otherwise a transferred agent
    keeps rows that still point at its previous owner."""

    def _write_set(self):
        from db.agent_settings.autonomy import TRANSFER_OWNER_COLUMNS

        return {(table.name, column) for table, column in TRANSFER_OWNER_COLUMNS}

    def test_the_guard_finds_owner_columns_at_all(self, conn):
        # Not vacuous: the enumeration sees the three columns known today.
        assert {("agent_ownership", "owner_id"), ("agent_schedules", "owner_id"),
                ("agent_reminders", "owner_id")} <= _owner_carrying_columns_in_metadata()

    def test_every_owner_carrying_column_in_metadata_is_transferred(self):
        assert _owner_carrying_columns_in_metadata() == self._write_set()

    def test_every_owner_carrying_column_in_the_sqlite_ddl_is_transferred(self, conn):
        assert _owner_carrying_columns_in_sqlite_ddl(conn) == self._write_set()

    def test_the_transfer_writes_every_registered_column(self, conn, ops):
        """The registry is what the transfer executes, not a list beside it."""
        from db.agent_settings.autonomy import TRANSFER_OWNER_COLUMNS

        assert ops.transfer_agent_ownership(
            CHILD, to_user_id=OWNER_B, expect=_found(), **_ALL_OFF
        ) is True
        for table, column in TRANSFER_OWNER_COLUMNS:
            values = [r[0] for r in conn.execute(
                f"SELECT {column} FROM {table.name} WHERE agent_name = ?", (CHILD,))]
            assert values == [OWNER_B], (table.name, column)
