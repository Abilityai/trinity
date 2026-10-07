"""trinity-enterprise#817 — proactive consent: consented / declined / not asked.

`agent_sharing.allow_proactive` defaulted to 0 for every share, so "nobody asked
this person" and "this person declined" were the same stored value. Now:
NULL = not asked, 0 = declined, 1 = consented (operator ruling 2026-10-06).

Pinned against the unit island's real SQLite:

* a new share is written NULL — "not asked", never the legacy DEFAULT 0;
* the Access toggle writes 0 / 1, read back as declined / consented;
* both read paths (the share list and the Access-tab operator list) say which;
* only `consented` lets an agent send unprompted — unchanged;
* the data migration moves existing 0s to NULL, leaves 1s, and is idempotent.
"""
import uuid

import pytest

pytestmark = pytest.mark.unit


def _agent():
    return f"agent-817-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def owner():
    """An admin who can share any agent, plus the agent's ownership row."""
    from database import db
    from db.engine import get_engine
    from db.tables import users
    from sqlalchemy import insert
    from utils.helpers import utc_now_iso

    name = f"owner-817-{uuid.uuid4().hex[:6]}"
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(
            username=name, role="admin", email=f"{name}@example.com",
            created_at=utc_now_iso(), updated_at=utc_now_iso()))
    agent = _agent()
    db.register_agent_owner(agent, name)
    return name, agent


def _stored(agent, email):
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().connect() as conn:
        return conn.execute(text(
            "SELECT allow_proactive FROM agent_sharing WHERE agent_name=:a AND shared_with_email=:e"),
            {"a": agent, "e": email}).scalar()


@pytest.mark.parametrize("value,state", [(None, "not_asked"), (0, "declined"), (1, "consented")])
def test_the_stored_value_reads_as_one_of_three_states(value, state):
    from db.agent_settings.sharing import SharingMixin
    assert SharingMixin.proactive_consent_state(value) == state


def test_a_new_share_is_not_asked_rather_than_declined(owner):
    from database import db
    name, agent = owner
    share = db.share_agent(agent, name, "Person@Example.com")
    assert share is not None
    assert _stored(agent, "person@example.com") is None
    assert share.proactive_consent == "not_asked" and share.allow_proactive is False


def test_the_toggle_records_an_answer_and_both_reads_say_which(owner):
    from database import db
    name, agent = owner
    db.share_agent(agent, name, "yes@example.com")
    db.share_agent(agent, name, "no@example.com")
    db.share_agent(agent, name, "unasked@example.com")
    assert db.set_allow_proactive(agent, "yes@example.com", True, name)
    assert db.set_allow_proactive(agent, "no@example.com", False, name)

    states = {r["email"]: r["proactive_consent"] for r in db.get_agent_operator_access(agent)}
    assert states == {"yes@example.com": "consented", "no@example.com": "declined",
                      "unasked@example.com": "not_asked"}
    shares = {s.shared_with_email: s.proactive_consent for s in db.get_agent_shares(agent)}
    assert shares == states


def test_only_consent_lets_an_agent_send(owner):
    from database import db
    name, agent = owner
    for email in ("yes@example.com", "no@example.com", "unasked@example.com"):
        db.share_agent(agent, name, email)
    db.set_allow_proactive(agent, "yes@example.com", True, name)
    db.set_allow_proactive(agent, "no@example.com", False, name)
    assert db.get_proactive_enabled_shares(agent) == ["yes@example.com"]


def test_the_migration_moves_legacy_zeros_to_not_asked_and_is_idempotent(owner):
    from database import db
    from db.connection import get_db_connection
    from db.engine import get_engine
    from db.migrations import _migrate_proactive_consent_not_asked
    from sqlalchemy import text
    name, agent = owner
    db.share_agent(agent, name, "legacy@example.com")
    db.share_agent(agent, name, "yes@example.com")
    with get_engine().begin() as conn:      # what every pre-#817 share looked like
        conn.execute(text("UPDATE agent_sharing SET allow_proactive = 0 "
                          "WHERE agent_name = :a AND shared_with_email = 'legacy@example.com'"),
                     {"a": agent})
    db.set_allow_proactive(agent, "yes@example.com", True, name)

    for _ in range(2):
        with get_db_connection() as conn:
            _migrate_proactive_consent_not_asked(conn.cursor(), conn)
    assert _stored(agent, "legacy@example.com") is None
    assert _stored(agent, "yes@example.com") == 1


def test_the_migration_is_registered_on_both_tracks():
    from pathlib import Path
    from db import migrations
    names = [n for n, _ in migrations.MIGRATIONS]
    assert "proactive_consent_not_asked" in names
    rev = (Path(migrations.__file__).resolve().parents[1]
           / "migrations" / "versions" / "0092_proactive_consent_not_asked.py").read_text()
    assert "allow_proactive = NULL WHERE allow_proactive = 0" in rev
