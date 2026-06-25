"""#17 — Access tab: operator (Trinity-user) access roster.

`get_agent_operator_access` resolves each `agent_sharing` allow-list email
against `users`: a row that resolves is an *active operator* (role/last-active
surfaced); an unresolved email is a *pending* invite. Verified against the real
engine (the unit conftest's initialized sqlite) so it exercises the actual
outer-join read path. Uses `ent17_`-prefixed names so it can't collide with
other tests sharing the per-process DB.
"""
from __future__ import annotations


def _engine():
    from db.engine import get_engine
    return get_engine()


def _insert_user(conn, *, username, email, role, last_login):
    from db.tables import users
    from utils.helpers import utc_now_iso
    conn.execute(users.insert().values(
        username=username, email=email, role=role,
        last_login=last_login, created_at=utc_now_iso(), updated_at=utc_now_iso(),
    ))


def _share(conn, *, agent, email, by_id):
    from db.tables import agent_sharing
    from utils.helpers import utc_now_iso
    conn.execute(agent_sharing.insert().values(
        agent_name=agent, shared_with_email=email.lower(),
        shared_by_id=by_id, created_at=utc_now_iso(), allow_proactive=0,
    ))


def test_operator_access_classifies_active_vs_pending():
    from database import db
    from db.tables import users

    with _engine().begin() as conn:
        # the sharer (owner) + an operator who has logged in
        _insert_user(conn, username="ent17_owner", email="ent17_owner@x.com", role="admin", last_login="2026-06-01T10:00:00Z")
        _insert_user(conn, username="ent17_op@x.com", email="ent17_op@x.com", role="creator", last_login="2026-06-20T09:00:00Z")
        owner_id = conn.execute(
            users.select().where(users.c.username == "ent17_owner")
        ).mappings().first()["id"]
        # active operator: shared email resolves to a users row
        _share(conn, agent="ent17_a1", email="ent17_op@x.com", by_id=owner_id)
        # pending invite: shared email with no users account
        _share(conn, agent="ent17_a1", email="ent17_invitee@x.com", by_id=owner_id)
        # different agent — must not leak into this agent's roster
        _share(conn, agent="ent17_other", email="ent17_op@x.com", by_id=owner_id)

    rows = db.get_agent_operator_access("ent17_a1")
    by_email = {r["email"]: r for r in rows}

    assert set(by_email) == {"ent17_op@x.com", "ent17_invitee@x.com"}, "scoped to the agent"

    active = by_email["ent17_op@x.com"]
    assert active["status"] == "active"
    assert active["username"] == "ent17_op@x.com"
    assert active["role"] == "creator"
    assert active["last_active"] == "2026-06-20T09:00:00Z"

    pending = by_email["ent17_invitee@x.com"]
    assert pending["status"] == "pending"
    assert pending["username"] is None
    assert pending["role"] is None
    assert pending["last_active"] is None


def test_operator_access_empty_for_unshared_agent():
    from database import db
    assert db.get_agent_operator_access("ent17_nobody_here") == []
