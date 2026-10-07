"""
The per-agent skill gate map — storage and the read seam (trinity-enterprise#753).

What these pin, and why each exists:

1. both migration tracks build `agent_skill_gates`: the SQLite entry is reached
   through the REGISTERED `MIGRATIONS` list (a function tested directly says
   nothing about registration) and run twice; the Alembic revision extends the
   single head;
2. the primary key `(agent_name, skill_name)` is declared in `db/tables.py` too,
   so an ON CONFLICT write works on a database built from either source;
3. the rows follow the agent — rename re-keys them, purge removes them — through
   the AGENT_REFS registry, over a real database;
4. the facade delegates every `SkillGateOperations` method with the same
   signature (tests that mock `db` cannot see a missing delegation);
5. `list_skill_gates` — the seam ent#751's dispatch check and ent#752's hook read
   — answers from the table: lowercased keys, tombstones skipped, and a failed
   read RAISES (never `{}`, which reads as "nothing is gated" and opens every
   skill), so `read_gates` refuses 503;
6. a gate written as `My-Skill` holds `/my-skill` through the real matcher.

Related flow: docs/memory/feature-flows/skill-gate.md
"""
import inspect
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend, seed_agent, seed_user  # noqa: E402,F401

pytestmark = pytest.mark.unit

FIN = "fin-753-storage"
SIB = "sib-753-storage"


@pytest.fixture
def gates_db(db_backend):
    from database import db
    seed_user(1, "owner-753-storage")
    seed_agent(FIN, 1)
    seed_agent(SIB, 1)
    return db


def _write(db, agent, skill, *, approver="primary", deadline=None, origin="set"):
    return db.write_skill_gate(agent, skill, approver=approver, deadline_hours=deadline,
                               origin=origin, set_by="owner-753-storage", set_by_agent=None)


# ---- 1. both tracks ------------------------------------------------------------

def test_the_registered_sqlite_migration_builds_the_table_idempotently(tmp_path):
    import sqlite3
    from db import migrations
    entry = dict(migrations.MIGRATIONS)["agent_skill_gates"]
    conn = sqlite3.connect(tmp_path / "m.db")
    cur = conn.cursor()
    entry(cur, conn)
    entry(cur, conn)
    cols = {r[1]: r for r in cur.execute("PRAGMA table_info(agent_skill_gates)")}
    assert set(cols) == {"agent_name", "skill_name", "approver", "deadline_hours", "origin",
                         "set_by", "set_by_agent", "set_at"}
    pk = sorted((r[5], r[1]) for r in cols.values() if r[5])
    assert [name for _, name in pk] == ["agent_name", "skill_name"]


def test_the_alembic_revision_extends_the_single_head():
    import importlib.util
    root = _BACKEND / "migrations" / "versions"
    spec = importlib.util.spec_from_file_location("rev753", root / "0093_agent_skill_gates.py")
    rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev)
    assert (rev.revision, rev.down_revision) == (
        "0093_agent_skill_gates", "0092_portal_messages_attachments")


def test_tables_metadata_declares_the_primary_key():
    from db.tables import agent_skill_gates
    assert [c.name for c in agent_skill_gates.primary_key.columns] == ["agent_name", "skill_name"]


def test_an_upsert_works_on_a_database_built_from_tables_py(tmp_path, monkeypatch):
    """ON CONFLICT needs the PK on the database the write runs against; one
    built from `tables.py` metadata must carry it as well as `schema.py`'s."""
    from sqlalchemy import create_engine, insert
    import db.tables as tables
    import db.skill_gates as mod
    engine = create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    tables.agent_ownership.create(engine)
    tables.agent_skills.create(engine)
    tables.agent_skill_sets.create(engine)      # the per-agent write lock touches it
    tables.agent_skill_gates.create(engine)
    with engine.begin() as conn:
        conn.execute(insert(tables.agent_ownership).values(
            agent_name=FIN, owner_id=1, created_at="2026-10-07T00:00:00Z"))
    monkeypatch.setattr(mod, "get_engine", lambda: engine)
    ops = mod.SkillGateOperations()
    ops.write_skill_gate(FIN, "pay-invoice", approver="primary", deadline_hours=None,
                         origin="set", set_by="o", set_by_agent=None)
    prev, cur, changed = ops.write_skill_gate(FIN, "pay-invoice", approver="primary", deadline_hours=48,
                                              origin="set", set_by="o", set_by_agent=None)
    assert prev["deadline_hours"] is None and cur["deadline_hours"] == 48 and changed
    assert [r["skill_name"] for r in ops.list_agent_skill_gates(FIN)] == ["pay-invoice"]


# ---- 2. the operations, on the real per-test database -------------------------

def test_write_lowercases_the_key_and_reports_previous_and_current(gates_db):
    prev, cur, changed = _write(gates_db, FIN, "Pay-Invoice")
    assert prev is None and changed
    assert cur["skill_name"] == "pay-invoice" and cur["approver"] == "primary"
    prev, cur, changed = _write(gates_db, FIN, "PAY-INVOICE", deadline=12)
    assert prev["deadline_hours"] is None and cur["deadline_hours"] == 12 and changed
    assert [r["skill_name"] for r in gates_db.list_agent_skill_gates(FIN)] == ["pay-invoice"]
    assert _write(gates_db, FIN, "pay-invoice", deadline=12)[2] is False     # same values: no write


def test_a_field_not_sent_keeps_what_is_stored(gates_db):
    """The merge happens under the lock, in the write itself: two partial
    writes never drop each other's field."""
    from db.skill_gates import KEEP
    _write(gates_db, FIN, "pay-invoice", approver="approver", deadline=8)
    gates_db.write_skill_gate(FIN, "pay-invoice", approver=KEEP, deadline_hours=4, origin="set",
                              set_by="o", set_by_agent=None)
    gates_db.write_skill_gate(FIN, "pay-invoice", approver="primary", deadline_hours=KEEP, origin="set",
                              set_by="o", set_by_agent=None)
    [row] = gates_db.list_agent_skill_gates(FIN)
    assert (row["approver"], row["deadline_hours"]) == ("primary", 4)


def test_a_write_to_an_agent_that_is_not_live_lands_nowhere(gates_db):
    """A rename or purge between the auth check and the insert must not leave a
    row under a name a future agent could inherit."""
    assert _write(gates_db, "no-such-agent-753", "pay-invoice") == (None, None, False)
    assert gates_db.list_agent_skill_gates("no-such-agent-753") == []


def test_rows_are_per_agent(gates_db):
    _write(gates_db, FIN, "pay-invoice")
    assert gates_db.list_agent_skill_gates(SIB) == []


# ---- 3. the rows follow the agent ---------------------------------------------

def test_the_gate_map_is_a_cascade_agent_ref():
    from db.agent_cleanup import AGENT_REFS, Policy
    refs = {(r.table, r.column): r.policy for r in AGENT_REFS}
    assert refs[("agent_skill_gates", "agent_name")] is Policy.CASCADE
    # audit-only provenance, deliberately not re-keyed (the assigned_by_agent precedent)
    assert ("agent_skill_gates", "set_by_agent") not in refs


def test_rename_carries_the_gates_and_purge_removes_them(gates_db):
    _write(gates_db, FIN, "pay-invoice")
    assert gates_db.rename_agent(FIN, FIN + "-renamed") is True
    assert gates_db.list_agent_skill_gates(FIN) == []
    assert [r["skill_name"] for r in gates_db.list_agent_skill_gates(FIN + "-renamed")] == ["pay-invoice"]
    gates_db.delete_agent_ownership(FIN + "-renamed")      # soft delete keeps them (recovery)
    assert len(gates_db.list_agent_skill_gates(FIN + "-renamed")) == 1
    gates_db.purge_agent_ownership(FIN + "-renamed")
    assert gates_db.list_agent_skill_gates(FIN + "-renamed") == []


# ---- 4. facade parity ----------------------------------------------------------

def test_the_facade_delegates_every_operation_with_the_same_signature():
    from database import DatabaseManager
    from db.skill_gates import SkillGateOperations
    names = [n for n, f in inspect.getmembers(SkillGateOperations, inspect.isfunction)
             if not n.startswith("_")]
    assert len(names) >= 5, names
    for name in names:
        assert hasattr(DatabaseManager, name), f"DatabaseManager has no {name}"
        ops_sig = inspect.signature(getattr(SkillGateOperations, name))
        facade_sig = inspect.signature(getattr(DatabaseManager, name))
        assert list(ops_sig.parameters) == list(facade_sig.parameters), name


# ---- 5. the read seam ----------------------------------------------------------

def test_list_skill_gates_reads_the_table_and_skips_tombstones(gates_db):
    from services import skill_gate_service as svc
    _write(gates_db, FIN, "Pay-Invoice", approver="approver", deadline=6)
    _write(gates_db, FIN, "deploy", origin="library_default")
    _write(gates_db, FIN, "refund", origin="cleared")
    gates = svc.list_skill_gates(FIN)
    assert gates == {
        "pay-invoice": svc.SkillGate(approver="approver", deadline_hours=6),
        "deploy": svc.SkillGate(approver="primary", deadline_hours=None),
    }
    assert svc.list_skill_gates(SIB) == {}


def test_a_failed_read_raises_and_read_gates_refuses(gates_db, monkeypatch):
    from database import db
    from services import skill_gate_service as svc

    def _boom(agent_name):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "list_agent_skill_gates", _boom)
    with pytest.raises(RuntimeError):
        svc.list_skill_gates(FIN)
    with pytest.raises(svc.SkillGateRefused) as e:
        svc.read_gates(FIN)
    assert (e.value.status_code, e.value.code) == (503, "gate_unavailable")


# ---- 6. the stored key holds through the real matchers -------------------------

def test_a_gate_written_mixed_case_holds_through_the_matchers(gates_db):
    from services import skill_gate_service as svc
    from utils.skill_invocation import find_gated_invocations
    _write(gates_db, FIN, "My-Skill")
    gates = svc.read_gates(FIN)
    assert find_gated_invocations("please run /my-skill now", gates) == ["my-skill"]
    assert find_gated_invocations("please run /MY-SKILL now", gates) == ["my-skill"]
    # the in-container hook's names are matched casefolded against the same keys
    hits = sorted(k for k in gates if k.casefold() in {"My-Skill".casefold()})
    assert hits == ["my-skill"]
