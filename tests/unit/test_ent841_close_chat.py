"""ent#841 — close a chat from its tab: archive it, never delete it.

The properties worth pinning:

  * **Archive, not delete.** Closing sets ``archived_at`` on the row and nothing
    else — the messages are still there, and reopening clears the flag.
  * **The live Main cannot be closed.** Reset is its retire action, so the
    refusal is a NAMED 409 (the chat exists and is theirs) rather than a 404.
  * **A retired Main reopens as an ordinary chat.** The pair already has a new
    Main, so ``is_main`` stays 0 — reopening must not race a second Main into
    the partial unique index.
  * **Scoped like rename.** Another client's id, or an unknown one, is the
    uniform 404 (Invariant #8) and changes nothing.
  * **Idempotent.** Closing twice keeps the first ``archived_at``.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

ALICE = "alice@example.com"
BOB = "bob@example.com"
AGENT = "scribe"
NOW = "2026-10-08T10:00:00Z"


@pytest.fixture()
def portal(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-close-chat.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from sqlalchemy import text
    from db.engine import get_engine
    from db import schema
    from db.tables import (
        metadata as oss_metadata,
        enterprise_portal_messages,
        enterprise_portal_sessions,
    )
    engine = get_engine()
    oss_metadata.create_all(engine, tables=[
        enterprise_portal_messages,
        enterprise_portal_sessions,
    ])
    with engine.begin() as conn:
        for stmt in schema.INDEXES:
            if "idx_portal_sessions_main" in stmt:
                conn.execute(text(stmt))

    from client_portal import service, db as pdb
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: True)
    return service, pdb


def _chat(service, pdb, email=ALICE):
    """An ordinary (non-Main) chat with one message in it."""
    sid = service.create_session(AGENT, email)["id"]
    pdb.add_portal_message(f"m-{sid}", AGENT, email, "user", "hello", None, NOW, session_id=sid)
    pdb.touch_portal_session(sid, NOW)
    return sid


def _row(pdb, sid, email=ALICE):
    return pdb.get_portal_session(sid, AGENT, email)


def test_close_archives_and_keeps_the_messages(portal):
    service, pdb = portal
    sid = _chat(service, pdb)

    out = service.set_session_archived(AGENT, ALICE, sid, True)

    assert out["archived_at"]
    assert _row(pdb, sid)["archived_at"] == out["archived_at"]
    # Nothing deleted: the row is listed and its message is still there.
    assert sid in [r["id"] for r in pdb.list_portal_sessions(AGENT, ALICE)]
    assert _row(pdb, sid)["message_count"] == 2


def test_reopen_clears_the_flag(portal):
    service, pdb = portal
    sid = _chat(service, pdb)
    service.set_session_archived(AGENT, ALICE, sid, True)

    out = service.set_session_archived(AGENT, ALICE, sid, False)

    assert out["archived_at"] is None
    assert _row(pdb, sid)["archived_at"] is None


def test_closing_twice_keeps_the_first_timestamp(portal, monkeypatch):
    service, pdb = portal
    sid = _chat(service, pdb)
    monkeypatch.setattr(service, "utc_now_iso", lambda: "2026-10-08T10:00:00Z")
    service.set_session_archived(AGENT, ALICE, sid, True)
    monkeypatch.setattr(service, "utc_now_iso", lambda: "2026-10-08T11:00:00Z")
    service.set_session_archived(AGENT, ALICE, sid, True)

    assert _row(pdb, sid)["archived_at"] == "2026-10-08T10:00:00Z"


def test_the_live_main_is_refused_by_name(portal):
    service, pdb = portal
    main = service.ensure_main_session(AGENT, ALICE)
    from client_portal.service import ClientPortalError

    with pytest.raises(ClientPortalError) as e:
        service.set_session_archived(AGENT, ALICE, main, True)
    assert e.value.status_code == 409
    assert "Reset" in e.value.detail
    assert not _row(pdb, main)["archived_at"]


def test_a_retired_main_reopens_as_an_ordinary_chat(portal, monkeypatch):
    service, pdb = portal
    main = service.ensure_main_session(AGENT, ALICE)
    pdb.add_portal_message("m1", AGENT, ALICE, "user", "hello", None, NOW, session_id=main)
    pdb.touch_portal_session(main, NOW)
    monkeypatch.setattr(service, "get_turn_inflight", lambda *_a, **_k: None)
    new_main = service.reset_main_session(AGENT, ALICE)["main_session_id"]

    out = service.set_session_archived(AGENT, ALICE, main, False)

    assert out["archived_at"] is None and out["is_main"] is False
    rows = {r["id"]: r for r in pdb.list_portal_sessions(AGENT, ALICE)}
    assert rows[main]["is_main"] == 0
    assert rows[new_main]["is_main"] == 1


def test_another_clients_chat_is_the_uniform_404(portal):
    service, pdb = portal
    bobs = _chat(service, pdb, email=BOB)
    from client_portal.service import ClientPortalError

    for sid in (bobs, "no-such-chat"):
        with pytest.raises(ClientPortalError) as e:
            service.set_session_archived(AGENT, ALICE, sid, True)
        assert e.value.status_code == 404
        assert e.value.detail == "Conversation not found"
    assert not _row(pdb, bobs, email=BOB)["archived_at"]


def test_an_agent_off_the_roster_is_404(portal, monkeypatch):
    service, pdb = portal
    sid = _chat(service, pdb)
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: False)
    from client_portal.service import ClientPortalError

    with pytest.raises(ClientPortalError) as e:
        service.set_session_archived(AGENT, ALICE, sid, True)
    assert e.value.status_code == 404
    assert not _row(pdb, sid)["archived_at"]


def test_routes_are_registered_for_close_and_reopen():
    from client_portal.router import router
    methods = {}
    for r in router.routes:
        if getattr(r, "path", "").endswith("/agents/{agent_name}/sessions/{session_id}/archive"):
            methods.update({m: r for m in r.methods})
    assert {"PUT", "DELETE"} <= set(methods)
