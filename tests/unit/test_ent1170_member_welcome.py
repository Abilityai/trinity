"""Hub onboarding seam: insert-once portal message and an inline-hook landing (abilityai/trinity-enterprise#1170).

The OSS half of the welcome feature, edition-agnostic:

* ``client_portal.db.add_portal_message_once`` — a deterministic-id insert
  that counts once, decided in SQL rather than by catching IntegrityError;
* the terms door's generic landing — an inline acceptance hook may return a
  same-origin path, and ``POST /api/terms/accept`` carries it as ``landing``.

The consumer's own behaviour is tested in the private submodule.
"""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest

pytestmark = pytest.mark.unit

def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _rows(msg_id: str):
    from db.engine import get_engine

    with get_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.exec_driver_sql(
            "SELECT * FROM enterprise_portal_messages WHERE id = ?", (msg_id,))]


def _message_count(session_id: str) -> int:
    from db.engine import get_engine

    with get_engine().connect() as conn:
        return conn.exec_driver_sql(
            "SELECT message_count FROM enterprise_portal_sessions WHERE id = ?",
            (session_id,)).scalar()


# --------------------------------------------------------------------------- #
# OSS: the insert-once helper
# --------------------------------------------------------------------------- #

class TestAddPortalMessageOnce:
    def test_a_second_write_inserts_nothing_and_counts_nothing(self):
        from client_portal import db as portal_db
        from client_portal import service as portal_service

        agent, email = _uniq("agent"), "member@example.com"
        session = portal_service.ensure_main_session(agent, email)
        msg_id = _uniq("member-welcome-7")

        first = portal_db.add_portal_message_once(msg_id, agent, email, "assistant", "hi",
                                                  "2026-11-08T00:00:00Z", session_id=session,
                                                  source="welcome")
        second = portal_db.add_portal_message_once(msg_id, agent, email, "assistant", "hi again",
                                                   "2026-11-08T00:00:01Z", session_id=session,
                                                   source="welcome")

        assert (first, second) == (True, False)
        rows = _rows(msg_id)
        assert len(rows) == 1 and rows[0]["content"] == "hi"
        assert rows[0]["role"] == "assistant" and rows[0]["source"] == "welcome"
        assert rows[0]["execution_id"] is None
        assert _message_count(session) == 1
        assert portal_db.portal_message_exists(msg_id) is True
        assert portal_db.portal_message_exists(_uniq("nope")) is False

    def test_it_does_not_rely_on_catching_an_integrity_error(self):
        """Decision #18: the duplicate is decided in SQL (ON CONFLICT DO
        NOTHING); a raised IntegrityError would surface here."""
        from client_portal import db as portal_db
        from client_portal import service as portal_service

        agent, email = _uniq("agent"), "member@example.com"
        session = portal_service.ensure_main_session(agent, email)
        msg_id = _uniq("dup")
        for _ in range(3):
            portal_db.add_portal_message_once(msg_id, agent, email, "assistant", "x",
                                              "2026-11-08T00:00:00Z", session_id=session)
        assert _message_count(session) == 1

    def test_the_conflict_target_is_the_primary_key_on_postgresql(self, monkeypatch):
        """PostgreSQL refuses ``ON CONFLICT (col)`` unless ``col`` carries a
        unique or primary-key constraint. The runs above prove it against the
        SQLite DDL; PostgreSQL is built from ``db/tables.py``. Record what the
        helper really executes, compile it for PostgreSQL, and check the
        conflict target and the binds against that definition."""
        import re

        from sqlalchemy.dialects import postgresql

        from client_portal import db as portal_db
        from db.tables import enterprise_portal_messages

        executed = []

        class _Result:
            rowcount = 1

        class _Conn:
            def execute(self, stmt, params=None):
                executed.append((stmt, params))
                return _Result()

        class _Begin:
            def __enter__(self):
                return _Conn()

            def __exit__(self, *exc):
                return False

        class _Engine:
            def begin(self):
                return _Begin()

        monkeypatch.setattr(portal_db, "get_engine", lambda: _Engine())
        assert portal_db.add_portal_message_once("m-1", "a", "x@example.com", "assistant", "hi",
                                                 "2026-11-08T00:00:00Z", session_id="s-1",
                                                 source="welcome") is True

        insert, params = executed[0]
        sql = str(insert.compile(dialect=postgresql.dialect()))
        assert sql.startswith("INSERT INTO enterprise_portal_messages")
        target = re.search(r"ON CONFLICT \((\w+)\) DO NOTHING\s*$", sql)
        assert target, sql
        pk = {c.name for c in enterprise_portal_messages.primary_key.columns}
        assert {target.group(1)} == pk
        # Every bind the statement names is supplied (a missing one is a
        # PostgreSQL-time error, not a SQLite one).
        assert set(re.findall(r"%\((\w+)\)s", sql)) <= set(params)


# --------------------------------------------------------------------------- #
# OSS: the terms door's landing seam
# --------------------------------------------------------------------------- #

@pytest.fixture
def clean_hooks():
    from services import terms_service

    terms_service.clear_acceptance_hooks()
    yield
    terms_service.clear_acceptance_hooks()


class TestLandingSeam:
    @pytest.mark.parametrize("value,expected", [
        ("/workspace?agent=member-7", "/workspace?agent=member-7"),
        ("/", "/"),
        ("//evil.example", None),
        ("/\\evil.example", None),
        ("https://evil.example/", None),
        ("workspace", None),
        ("/work\nspace", None),
        ("/" + "a" * 600, None),
        ("", None),
        (None, None),
        (42, None),
    ])
    def test_only_a_same_origin_path_is_a_landing(self, value, expected):
        from services.terms_service import landing_path

        assert landing_path(value) == expected

    @pytest.mark.asyncio
    async def test_the_first_hook_that_names_a_path_wins(self, clean_hooks):
        from services import terms_service as ts

        async def silent(*_a):
            return None

        async def bad(*_a):
            return "//evil.example"

        async def names(*_a):
            return "/workspace?agent=member-7"

        async def later(*_a):
            return "/workspace?agent=other"

        for fn in (silent, bad, names, later):
            ts.register_acceptance_hook(fn, inline=True)
        assert await ts.run_inline_acceptance_hooks(7, "m@example.com", "v1", "t") \
            == "/workspace?agent=member-7"

    @pytest.mark.asyncio
    async def test_no_hooks_no_landing(self, clean_hooks):
        from services import terms_service as ts

        assert await ts.run_inline_acceptance_hooks(7, "m@example.com", "v1", "t") is None

    @pytest.mark.asyncio
    async def test_the_accept_route_carries_the_landing(self, clean_hooks, monkeypatch):
        from fastapi import BackgroundTasks

        from models import TermsAccept, TermsAccepted, User
        from routers import terms as terms_router
        from services import terms_gate, terms_service as ts

        async def inline(*_a):
            return "/workspace?agent=member-7"

        ts.register_acceptance_hook(inline, inline=True)

        async def _accept(*_a, **_k):
            return {"accepted": True, "created": True, "version": "v1",
                    "accepted_at": "2026-11-08T00:00:00Z", "user_id": 7,
                    "email": "m@example.com"}

        monkeypatch.setattr(ts, "accept", _accept)
        monkeypatch.setattr(terms_gate, "assert_door_open", lambda *_a: None)
        monkeypatch.setattr(terms_gate, "provider_door", lambda *_a: None)
        monkeypatch.setattr(terms_router, "reject_non_interactive_principal", lambda *_a: None)
        request = types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"),
                                        state=types.SimpleNamespace(request_id="r"),
                                        scope={"path": "/api/terms/accept"})
        out = await terms_router.accept_terms(
            TermsAccept(version="v1"), request, BackgroundTasks(),
            current_user=User(id=7, username="m", email="m@example.com", role="user"))
        assert TermsAccepted(**out).landing == "/workspace?agent=member-7"
