"""trinity-enterprise#962 — the owner's view of a person's memory, with undo.
Related flow: docs/memory/feature-flows/public-agent-links.md (MEM-001)

ent#637 gave the PERSON a history of their memory and an undo, in the
Workspace. Nobody else could read it, and the summarizer's half of the memory
was never recorded at all. This suite pins, at the layer each change lives in:

1. **The readers rule** (the ent#638 seat-decision rule), through the real
   FastAPI dependency chain: owner yes, admin yes; a person the agent is shared
   with, an approver, an agent key, a system key and a connector key no.
2. **Summarizer history**: a summary change is recorded as a `summary` row,
   only when the text changed, and it is a compare-and-set against the summary
   it was built from (an undo during the model call is never overwritten).
3. **Undo is latest-first PER SECTION**, through ONE primitive the person and
   the operator share; the operator's undo is stamped and the person's view
   says so without naming who.
4. **Paging** with a cursor, the **people list** union, and reads that never
   create a memory row.
5. The dual-track **migration**.

Self-contained: the unit conftest's real SQLite, no live backend.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from database import db
from db_models import UserCreate
from dependencies import get_current_user
from models import User
from routers import person_memory

# The router and the dependency it is overridden on are imported TOGETHER at
# collection: the unit conftest restores a baseline `dependencies` module
# around every test, so a router imported inside a test can hold a different
# `get_current_user` than a later `from dependencies import …` returns.

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _user(username, *, role="creator", email=None, mcp_scope=None, agent_name=None, connector_agent=None):
    return User(id=1, username=username, role=role, email=email, mcp_scope=mcp_scope,
                agent_name=agent_name, connector_agent=connector_agent,
                created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z")


@pytest.fixture
def agent():
    suffix = uuid.uuid4().hex[:8]
    owner = f"owner-{suffix}@example.com"
    admin = f"admin-{suffix}"
    shared = f"shared-{suffix}@example.com"
    approver = f"approver-{suffix}@example.com"
    db.create_user(UserCreate(username=owner, email=owner, role="creator", password="x"))
    db.create_user(UserCreate(username=admin, email=f"{admin}@example.com", role="admin", password="x"))
    db.create_user(UserCreate(username=shared, email=shared, role="creator", password="x"))
    db.create_user(UserCreate(username=approver, email=approver, role="creator", password="x"))
    name = f"pm-agent-{suffix}"
    db.register_agent_owner(name, owner)
    db.share_agent(name, owner, shared)
    return SimpleNamespace(name=name, owner=owner, admin=admin, shared=shared, approver=approver)


def _notes(agent_name, email, text, *, execution_id="e1", triggered_by="public", schedule_id=None):
    return db.write_public_user_memory_agent_notes(
        agent_name, email, text, execution_id=execution_id, triggered_by=triggered_by, schedule_id=schedule_id)


def _summary(agent_name, email, text, **kw):
    return db.update_public_user_memory_conversation_summary(agent_name, email, text, **kw)


def _client(principal):
    app = FastAPI()
    app.include_router(person_memory.router)
    app.dependency_overrides[get_current_user] = lambda: principal
    return TestClient(app, raise_server_exceptions=True)


# --------------------------------------------------------------------------- #
# 1. The readers rule — through the real dependency chain
# --------------------------------------------------------------------------- #

class TestReadersRule:
    def _calls(self, agent, person="ann@example.com", write_id="nope"):
        base = f"/api/agents/{agent.name}/person-memory"
        return [
            ("GET", base, None),
            ("GET", f"{base}/person", {"user_email": person}),
            ("GET", f"{base}/writes", {"user_email": person}),
            ("POST", f"{base}/writes/{write_id}/undo", None),
        ]

    def _do(self, client, method, path, params, person="ann@example.com"):
        if method == "POST":
            return client.post(path, json={"user_email": person})
        return client.get(path, params=params)

    def test_owner_and_admin_read_every_person(self, agent):
        _notes(agent.name, "ann@example.com", "Prefers PDF invoices.")
        for principal in (_user(agent.owner, email=agent.owner),
                          _user(agent.admin, role="admin", email=f"{agent.admin}@example.com")):
            c = _client(principal)
            people = c.get(f"/api/agents/{agent.name}/person-memory").json()
            assert [p["user_email"] for p in people["people"]] == ["ann@example.com"]
            detail = c.get(f"/api/agents/{agent.name}/person-memory/person",
                           params={"user_email": "Ann@Example.com"}).json()
            assert detail["notes"] == "Prefers PDF invoices."
            writes = c.get(f"/api/agents/{agent.name}/person-memory/writes",
                           params={"user_email": "ann@example.com"}).json()
            assert len(writes["writes"]) == 1

    def test_a_shared_user_and_an_approver_get_the_uniform_404(self, agent, monkeypatch):
        """Assignment is not access (ent#500): an approver the provider names
        gains nothing, and a share is "may use", never "may read others"."""
        from services import assignment_provider

        class _Approver:
            def kinds_for(self, agent_name, email):
                return {"kind": "approver"}
        monkeypatch.setattr(assignment_provider, "_provider", _Approver())
        w = _notes(agent.name, "ann@example.com", "v1")
        for username in (agent.shared, agent.approver):
            c = _client(_user(username, email=username))
            for method, path, params in self._calls(agent, write_id=w["write_id"]):
                r = self._do(c, method, path, params)
                assert r.status_code == 404, (username, method, path, r.status_code)
        assert db.get_public_user_memory_agent_notes(agent.name, "ann@example.com") == "v1"

    def test_machine_principals_are_refused_before_any_lookup(self, agent):
        """Agent, system and connector keys are refused 403 on the principal
        alone — the same answer for this agent and for one that does not
        exist, so the refusal discloses nothing about the target (#186)."""
        machines = [
            _user(agent.owner, email=agent.owner, mcp_scope="agent", agent_name=agent.name),
            _user(agent.owner, email=agent.owner, mcp_scope="system"),
            _user(agent.owner, email=agent.owner, mcp_scope="connector", connector_agent=agent.name),
        ]
        ghost = SimpleNamespace(name="no-such-agent-962")
        for principal in machines:
            c = _client(principal)
            for target in (agent, ghost):
                for method, path, params in self._calls(target):
                    r = self._do(c, method, path, params)
                    assert r.status_code == 403, (principal.mcp_scope, target.name, method, path, r.status_code)

    def test_the_owners_user_scoped_key_reads_as_on_the_facts_routes(self, agent):
        """`require_person` admits a user-scoped key (the human's own tooling),
        exactly as the MEM-002 facts routes do. Pinned so a change is a
        decision, not a drift."""
        _notes(agent.name, "ann@example.com", "v1")
        c = _client(_user(agent.owner, email=agent.owner, mcp_scope="user"))
        assert c.get(f"/api/agents/{agent.name}/person-memory").status_code == 200


# --------------------------------------------------------------------------- #
# 2. Summarizer history + compare-and-set
# --------------------------------------------------------------------------- #

class TestSummarizerHistory:
    def test_a_summary_change_is_recorded_and_a_no_op_is_not(self, agent):
        assert _summary(agent.name, "ann@example.com", "- Likes PDFs") is True
        assert _summary(agent.name, "ann@example.com", "- Likes PDFs") is True   # unchanged: stored, no row
        assert _summary(agent.name, "ann@example.com", "- Likes PDFs\n- Based in Lyon") is True
        rows = db.list_public_user_memory_writes(agent.name, "ann@example.com", 20, section=None)
        assert [(r["section"], r["triggered_by"]) for r in rows] == [("summary", "summarizer")] * 2
        assert rows[0]["previous_notes"] == "- Likes PDFs"
        assert rows[0]["new_notes"].endswith("Lyon")
        # The person's default list stays notes-only.
        assert db.list_public_user_memory_writes(agent.name, "ann@example.com", 20) == []

    def test_a_summary_built_on_a_stale_base_is_dropped(self, agent):
        _summary(agent.name, "ann@example.com", "base")
        assert _summary(agent.name, "ann@example.com", "rebuilt", expected_previous="something else") is False
        assert db.get_or_create_public_user_memory(agent.name, "ann@example.com")["conversation_summary"] == "base"
        assert len(db.list_public_user_memory_writes(agent.name, "ann@example.com", 20, section=None)) == 1

    def test_an_undo_during_the_model_call_survives_the_summarizer(self, agent, monkeypatch, caplog):
        """The real `summarize_user_memory_background`: the operator undoes the
        poisoned summary while the model is answering; the summarizer's write,
        built from the poisoned base, must be dropped, not land over the undo."""
        from services import model_gateway
        from services import platform_prompt_service as pps

        email = "ann@example.com"
        _summary(agent.name, email, "- clean")
        poisoned = _summary(agent.name, email, "- clean\n- poisoned line")
        assert poisoned is True
        latest = db.list_public_user_memory_writes(agent.name, email, 1, section="summary")[0]

        monkeypatch.setattr(model_gateway, "resolve", lambda a: SimpleNamespace(usable=True, reason=None))
        monkeypatch.setattr(db, "get_recent_public_chat_messages",
                            lambda *a, **k: [SimpleNamespace(role="user", content="hi")])

        async def complete(*a, **k):
            assert db.undo_public_user_memory_write(agent.name, email, latest["id"], undone_by="op@example.com") == "undone"
            return "- clean\n- poisoned line\n- new fact"
        monkeypatch.setattr(model_gateway, "complete", complete)

        with caplog.at_level(logging.INFO):
            asyncio.run(pps.summarize_user_memory_background(agent.name, email, "sess-1"))
        assert db.get_or_create_public_user_memory(agent.name, email)["conversation_summary"] == "- clean"
        rows = db.list_public_user_memory_writes(agent.name, email, 20, section="summary")
        assert len(rows) == 2 and rows[0]["undone_at"] is not None
        # #4275 review I1: a dropped write is never logged as an update.
        assert not [r for r in caplog.records if "Updated conversation_summary" in r.getMessage()]


# --------------------------------------------------------------------------- #
# 3. Undo — per section, one primitive, the operator stamped
# --------------------------------------------------------------------------- #

class TestUndo:
    def test_undo_is_latest_first_per_section(self, agent):
        email = "ann@example.com"
        n1 = _notes(agent.name, email, "notes v1")
        _summary(agent.name, email, "summary v1")
        # A later summary write does not block undoing the latest NOTES write.
        assert db.undo_public_user_memory_write(agent.name, email, n1["write_id"], undone_by=email) == "undone"
        rec = db.get_or_create_public_user_memory(agent.name, email)
        assert (rec["agent_notes"], rec["conversation_summary"]) == ("", "summary v1")
        s = db.list_public_user_memory_writes(agent.name, email, 1, section="summary")[0]
        assert db.undo_public_user_memory_write(agent.name, email, s["id"], undone_by=email) == "undone"
        rec = db.get_or_create_public_user_memory(agent.name, email)
        assert (rec["agent_notes"], rec["conversation_summary"]) == ("", "")

    def test_within_a_section_a_later_write_still_blocks(self, agent):
        email = "ann@example.com"
        _summary(agent.name, email, "s1")
        _summary(agent.name, email, "s2")
        older = db.list_public_user_memory_writes(agent.name, email, 2, section="summary")[1]
        assert db.undo_public_user_memory_write(agent.name, email, older["id"], undone_by=email) == "not_latest"

    def test_the_operator_undo_goes_through_the_person_primitive(self, agent, monkeypatch):
        """ONE undo primitive: the owner route and the Workspace both end in
        `db.undo_public_user_memory_write`, with `undone_by` = who acted."""
        calls = []
        real = db.undo_public_user_memory_write

        def spy(agent_name, email, write_id, *, undone_by, section=None):
            calls.append((email, undone_by, section))
            return real(agent_name, email, write_id, undone_by=undone_by, section=section)
        monkeypatch.setattr(db, "undo_public_user_memory_write", spy)

        w = _notes(agent.name, "ann@example.com", "v1")
        c = _client(_user(agent.owner, email=agent.owner))
        r = c.post(f"/api/agents/{agent.name}/person-memory/writes/{w['write_id']}/undo",
                   json={"user_email": "ann@example.com"})
        assert r.status_code == 200, r.text
        assert r.json()["notes"] == ""

        from client_portal import agent_page
        w2 = _notes(agent.name, "ann@example.com", "v2")
        agent_page.undo_memory_write(agent.name, "ann@example.com", w2["write_id"])
        assert calls == [("ann@example.com", agent.owner, None), ("ann@example.com", "ann@example.com", "notes")]

    def test_refusals_are_named_on_the_owner_route(self, agent):
        email = "ann@example.com"
        w1 = _notes(agent.name, email, "v1")
        _notes(agent.name, email, "v2")
        c = _client(_user(agent.owner, email=agent.owner))
        url = f"/api/agents/{agent.name}/person-memory/writes/{w1['write_id']}/undo"
        r = c.post(url, json={"user_email": email})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_latest"
        # Another person's write id, or a person with no memory here: the uniform 404.
        r = c.post(url, json={"user_email": "bob@example.com"})
        assert r.status_code == 404

    def test_the_person_sees_an_operator_undo_without_who(self, agent):
        from client_portal import agent_page
        email = "ann@example.com"
        w1 = _notes(agent.name, email, "v1")
        w2 = _notes(agent.name, email, "v2")
        c = _client(_user(agent.owner, email=agent.owner))
        assert c.post(f"/api/agents/{agent.name}/person-memory/writes/{w2['write_id']}/undo",
                      json={"user_email": email}).status_code == 200
        # The person's own undo, recorded with a differently-cased address.
        assert db.undo_public_user_memory_write(agent.name, email, w1["write_id"], undone_by="Ann@Example.com") == "undone"
        page = agent_page.memory(agent.name, email)
        by_id = {w["id"]: w for w in page["writes"]}
        assert by_id[w2["write_id"]]["undone_by_operator"] is True
        assert by_id[w1["write_id"]]["undone_by_operator"] is False
        assert all("undone_by" not in w for w in page["writes"])       # never who

    def test_the_person_cannot_undo_a_summary_write(self, agent):
        from client_portal import agent_page
        email = "ann@example.com"
        _summary(agent.name, email, "s1")
        s = db.list_public_user_memory_writes(agent.name, email, 1, section="summary")[0]
        with pytest.raises(agent_page.MemoryUndoRefused) as ei:
            agent_page.undo_memory_write(agent.name, email, s["id"])
        assert ei.value.status_code == 404
        assert db.get_or_create_public_user_memory(agent.name, email)["conversation_summary"] == "s1"


# --------------------------------------------------------------------------- #
# 4. Paging, the people list, non-creating reads
# --------------------------------------------------------------------------- #

class TestPagingAndPeople:
    def test_history_pages_with_a_cursor_that_survives_new_writes(self, agent):
        from services import person_memory_service as pms
        email = "ann@example.com"
        ids = [_notes(agent.name, email, f"v{i}", execution_id=f"e{i}")["write_id"] for i in range(25)]
        first = pms.history_page(agent.name, email, limit=20)
        assert len(first["writes"]) == 20 and first["next_before"]
        _notes(agent.name, email, "arrives while paging")          # lands at the top
        second = pms.history_page(agent.name, email, limit=20, before=first["next_before"])
        seen = [w["id"] for w in first["writes"]] + [w["id"] for w in second["writes"]]
        assert sorted(seen) == sorted(ids) and len(set(seen)) == 25
        assert second["next_before"] is None

    def test_the_persons_workspace_view_pages_its_notes(self, agent):
        """Ruling 7: the person pages through ALL their writes too, notes only."""
        from client_portal import agent_page
        email = "ann@example.com"
        for i in range(21):
            _notes(agent.name, email, f"v{i}", execution_id=f"e{i}")
        _summary(agent.name, email, "a summary the person's view never lists")
        first = agent_page.memory(agent.name, email)
        assert len(first["writes"]) == agent_page.MAX_MEMORY_WRITES and first["next_before"]
        assert {w["kind"] for w in first["writes"]} == {"conversation"}
        second = agent_page.memory(agent.name, email, before=first["next_before"])
        assert [w["notes"] for w in second["writes"]] == ["v0"] and second["next_before"] is None
        with pytest.raises(agent_page.InvalidMemoryCursor):
            agent_page.memory(agent.name, email, before="garbage")

    def test_undoable_is_decided_beyond_the_page(self, agent):
        """A run of undos pushes the latest open write onto page 2; the flag
        comes from the store, not from the slice."""
        from services import person_memory_service as pms
        email = "ann@example.com"
        ws = [_notes(agent.name, email, f"v{i}", execution_id=f"e{i}")["write_id"] for i in range(22)]
        for wid in reversed(ws[2:]):
            assert db.undo_public_user_memory_write(agent.name, email, wid, undone_by=email) == "undone"
        first = pms.history_page(agent.name, email, limit=20)
        assert not any(w["undoable"] for w in first["writes"])
        second = pms.history_page(agent.name, email, limit=20, before=first["next_before"])
        assert [w["id"] for w in second["writes"] if w["undoable"]] == [ws[1]]

    def test_a_bad_cursor_is_named(self, agent):
        _notes(agent.name, "ann@example.com", "v1")
        c = _client(_user(agent.owner, email=agent.owner))
        r = c.get(f"/api/agents/{agent.name}/person-memory/writes",
                  params={"user_email": "ann@example.com", "before": "garbage"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_cursor"

    def test_the_people_list_is_everyone_with_memory(self, agent):
        from services import person_memory_service as pms
        from services import memory_service as ms
        _notes(agent.name, "notes@example.com", "Has notes.")
        _summary(agent.name, "summary@example.com", "- Has a summary")
        w = _notes(agent.name, "cleared@example.com", "gone soon")
        db.undo_public_user_memory_write(agent.name, "cleared@example.com", w["write_id"], undone_by="x")
        db.insert_memory_fact(agent_name=agent.name, user_email="facts@example.com", text="Lives in Lyon",
                              text_hash=ms.fact_hash("Lives in Lyon"), embedding=b"\0" * 8,
                              embedding_model="m", source="public")
        db.get_or_create_public_user_memory(agent.name, "chatted@example.com")   # an empty row: no memory
        db.increment_public_user_memory_count(agent.name, "notes@example.com")   # a message bump: not a change
        page = pms.list_people(agent.name)
        emails = {p["user_email"] for p in page["people"]}
        assert emails == {"notes@example.com", "summary@example.com", "cleared@example.com", "facts@example.com"}
        assert page["total"] == 4
        by = {p["user_email"]: p for p in page["people"]}
        assert by["facts@example.com"]["fact_count"] == 1 and by["facts@example.com"]["write_count"] == 0
        assert by["notes@example.com"]["has_notes"] and not by["notes@example.com"]["has_summary"]
        assert by["summary@example.com"]["has_summary"] and by["summary@example.com"]["write_count"] == 1
        assert by["cleared@example.com"]["has_notes"] is False and by["cleared@example.com"]["write_count"] == 1
        # "Last change" is the newest write or fact, never the message bump.
        notes_write = db.list_public_user_memory_writes(agent.name, "notes@example.com", 1)[0]
        assert by["notes@example.com"]["last_changed_at"] == notes_write["written_at"]

    def test_a_person_whose_every_fact_was_forgotten_leaves_the_list(self, agent):
        """#4275 review I2: the union counts LIVE facts only."""
        from services import person_memory_service as pms
        from services import memory_service as ms
        fid = db.insert_memory_fact(agent_name=agent.name, user_email="gone@example.com", text="Lives in Lyon",
                                    text_hash=ms.fact_hash("Lives in Lyon"), embedding=b"\0" * 8,
                                    embedding_model="m", source="public")
        assert {p["user_email"] for p in pms.list_people(agent.name)["people"]} == {"gone@example.com"}
        assert db.soft_delete_memory_fact(fid, agent.name, deleted_by="owner")
        page = pms.list_people(agent.name)
        assert page["people"] == [] and page["total"] == 0

    def test_the_owners_own_row_points_at_the_agent_scope(self, agent):
        from services import person_memory_service as pms
        _notes(agent.name, agent.owner, "The owner chatted on a public link.")
        by = {p["user_email"]: p for p in pms.list_people(agent.name)["people"]}
        assert by[agent.owner]["agent_scope"] is True and by[agent.owner]["fact_count"] is None

    def test_an_owner_read_never_creates_a_memory_row(self, agent):
        c = _client(_user(agent.owner, email=agent.owner))
        r = c.get(f"/api/agents/{agent.name}/person-memory/person", params={"user_email": "nobody@example.com"})
        assert r.status_code == 404
        assert db.get_public_user_memory(agent.name, "nobody@example.com") is None

    def test_channel_identity_rides_along_where_a_request_captured_it(self, agent):
        from services import person_memory_service as pms
        _notes(agent.name, "tg@example.com", "Telegram person.")
        db.upsert_access_request(agent.name, "tg@example.com", "telegram",
                                 identity={"requester_channel_id": "42", "requester_handle": "anntg",
                                           "requester_name": "Ann T"})
        by = {p["user_email"]: p for p in pms.list_people(agent.name)["people"]}
        assert (by["tg@example.com"]["channel"], by["tg@example.com"]["channel_handle"],
                by["tg@example.com"]["channel_name"]) == ("telegram", "anntg", "Ann T")


# --------------------------------------------------------------------------- #
# 5. Wiring — the real app registers the four routes
# --------------------------------------------------------------------------- #

class TestWiring:
    def test_the_real_app_serves_the_owner_routes(self):
        """Every test above builds its own app around the router; this one
        reads the LIVE route table (`main.app`, in a subprocess via the census
        helper), so deleting the `include_router` line in main.py goes red."""
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _route_census as rc
        live = rc.runtime_paths(rc.runtime_routes())
        base = "/api/agents/{agent_name}/person-memory"
        assert live["routers/person_memory.py::list_person_memory_people"] == [f"GET {base}"]
        assert live["routers/person_memory.py::get_person_memory"] == [f"GET {base}/person"]
        assert live["routers/person_memory.py::list_person_memory_writes"] == [f"GET {base}/writes"]
        assert live["routers/person_memory.py::undo_person_memory_write"] == [f"POST {base}/writes/{{write_id}}/undo"]


# --------------------------------------------------------------------------- #
# 6. Migration — both tracks
# --------------------------------------------------------------------------- #

class TestMigration:
    def test_the_sqlite_migration_adds_the_section_with_notes_as_default(self):
        from db import migrations
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("""CREATE TABLE public_user_memory_writes (
            id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, user_email TEXT NOT NULL,
            execution_id TEXT, triggered_by TEXT NOT NULL, schedule_id TEXT,
            previous_notes TEXT NOT NULL DEFAULT '', new_notes TEXT NOT NULL DEFAULT '',
            written_at TEXT NOT NULL, undone_at TEXT, undone_by TEXT)""")
        cur.execute("INSERT INTO public_user_memory_writes (id, agent_name, user_email, triggered_by, written_at) "
                    "VALUES ('w1', 'a', 'p@example.com', 'public', 't')")
        migrations._migrate_public_user_memory_writes_section(cur, conn)
        migrations._migrate_public_user_memory_writes_section(cur, conn)     # idempotent
        assert cur.execute("SELECT section FROM public_user_memory_writes").fetchone() == ("notes",)
        assert "public_user_memory_writes_section" in [n for n, _ in migrations.MIGRATIONS]

    def test_the_alembic_downgrade_never_leaves_summary_text_to_read_as_notes(self, tmp_path):
        """#4275 review (low): dropping `section` would make a later upgrade label
        every summary row `notes`, and an Undo on one would restore summary text
        into the notes. The downgrade deletes the summary rows first. Driven
        through Alembic's real Operations on a SQLite file."""
        import importlib.util
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, text

        path = REPO / "src/backend/migrations/versions/0120_public_user_memory_writes_section.py"
        spec = importlib.util.spec_from_file_location("rev0120", path)
        rev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rev)
        engine = create_engine(f"sqlite:///{tmp_path / 'a.db'}")
        with engine.begin() as conn:
            conn.execute(text("""CREATE TABLE public_user_memory_writes (
                id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, user_email TEXT NOT NULL,
                execution_id TEXT, triggered_by TEXT NOT NULL, schedule_id TEXT,
                previous_notes TEXT NOT NULL DEFAULT '', new_notes TEXT NOT NULL DEFAULT '',
                written_at TEXT NOT NULL, undone_at TEXT, undone_by TEXT)"""))
            with Operations.context(MigrationContext.configure(conn)):
                rev.upgrade()
            conn.execute(text("INSERT INTO public_user_memory_writes (id, agent_name, user_email, triggered_by, written_at, section) "
                              "VALUES ('n1','a','p@example.com','public','t','notes'), ('s1','a','p@example.com','summarizer','t','summary')"))
            with Operations.context(MigrationContext.configure(conn)):
                rev.downgrade()
            assert [r[0] for r in conn.execute(text("SELECT id FROM public_user_memory_writes"))] == ["n1"]
            with Operations.context(MigrationContext.configure(conn)):
                rev.upgrade()
            assert conn.execute(text("SELECT section FROM public_user_memory_writes")).fetchall() == [("notes",)]

    def test_the_alembic_revision_chains_after_0119(self):
        path = REPO / "src/backend/migrations/versions/0120_public_user_memory_writes_section.py"
        src = path.read_text()
        assert 'down_revision = "0119_terms_acceptances"' in src
        assert 'revision = "0120_public_user_memory_writes_section"' in src
