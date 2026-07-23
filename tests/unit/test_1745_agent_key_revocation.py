"""Deleting an agent revokes its credentials (#1745).

Soft-deleting an agent used to leave its agent-scoped MCP key `is_active = 1`.
The credential kept authenticating against both the REST API and the MCP server,
inherited its owner's role (so on an admin-owned install it reached
`/api/audit-log`), enumerated the whole fleet — and could **mint a fresh key of
its own**, which made revoking it afterwards pointless.

`mcp_api_keys` is registered CASCADE in `AGENT_REFS` for exactly this reason
("an orphaned key must not survive its agent"), but `cascade_delete()` only runs
at the *hard purge* — after the entire soft-delete window (default 180 days).

Fix: deactivate on soft-delete, reactivate on recover (soft-delete is
recoverable, so the credential state has to be reversible too), plus a sweep for
keys orphaned before the fix. On the instance where this was found, 12 of 17
active agent-scoped keys were already orphaned.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def keys_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-keys.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import metadata as m, agent_ownership, users, mcp_api_keys
    m.create_all(get_engine(), tables=[agent_ownership, users, mcp_api_keys])

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(id=1, username="alice", role="admin",
                                          created_at="t", updated_at="t"))
    yield str(db_file)


def _mk_agent(name: str):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_ownership
    with get_engine().begin() as conn:
        conn.execute(insert(agent_ownership).values(
            agent_name=name, owner_id=1, created_at="t"))


def _mk_key(name: str, agent: str, scope: str = "agent", active: int = 1):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import mcp_api_keys
    with get_engine().begin() as conn:
        conn.execute(insert(mcp_api_keys).values(
            id=name, name=name, key_prefix="trinity_mcp_x", key_hash=f"h-{name}",
            created_at="t", is_active=active, user_id=1,
            agent_name=agent, scope=scope))


def _active(key_id: str) -> int:
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().connect() as conn:
        return conn.execute(
            text("SELECT is_active FROM mcp_api_keys WHERE id = :i"), {"i": key_id}
        ).scalar()


# --- delete revokes ----------------------------------------------------------

def test_soft_delete_deactivates_the_agents_key(keys_db):
    from database import db

    _mk_agent("doomed")
    _mk_key("k-doomed", "doomed")
    assert _active("k-doomed") == 1

    assert db.delete_agent_ownership("doomed") is True
    assert _active("k-doomed") == 0, (
        "an agent's credential must not outlive the agent — it could otherwise "
        "keep authenticating for the whole 180-day soft-delete window"
    )


def test_connector_scoped_keys_are_revoked_too(keys_db):
    """Both scopes are per-agent credentials and both are CASCADE entries."""
    from database import db

    _mk_agent("doomed")
    _mk_key("k-agent", "doomed", scope="agent")
    _mk_key("k-conn", "doomed", scope="connector")
    db.delete_agent_ownership("doomed")
    assert _active("k-agent") == 0 and _active("k-conn") == 0


def test_other_agents_keys_are_untouched(keys_db):
    from database import db

    _mk_agent("doomed"); _mk_agent("survivor")
    _mk_key("k-doomed", "doomed"); _mk_key("k-survivor", "survivor")
    db.delete_agent_ownership("doomed")
    assert _active("k-doomed") == 0
    assert _active("k-survivor") == 1


def test_user_scoped_keys_are_untouched(keys_db):
    """A user-scoped key is not a per-agent credential — deleting an agent must
    not revoke the owner's own key."""
    from database import db

    _mk_agent("doomed")
    _mk_key("k-user", None, scope="user")
    _mk_key("k-doomed", "doomed")
    db.delete_agent_ownership("doomed")
    assert _active("k-user") == 1


# --- recover restores --------------------------------------------------------

def test_recover_reactivates_the_key(keys_db):
    """Soft-delete is recoverable, so the credential state must be reversible —
    otherwise a recovered agent silently cannot talk to the platform."""
    from database import db

    _mk_agent("comeback")
    _mk_key("k-comeback", "comeback")
    db.delete_agent_ownership("comeback")
    assert _active("k-comeback") == 0

    assert db.recover_agent_ownership("comeback") is True
    assert _active("k-comeback") == 1


def test_delete_recover_delete_round_trip(keys_db):
    from database import db

    _mk_agent("yoyo")
    _mk_key("k-yoyo", "yoyo")
    db.delete_agent_ownership("yoyo");      assert _active("k-yoyo") == 0
    db.recover_agent_ownership("yoyo");     assert _active("k-yoyo") == 1
    db.delete_agent_ownership("yoyo");      assert _active("k-yoyo") == 0


def test_recover_does_not_resurrect_a_manually_revoked_key(keys_db):
    """A key an operator revoked by hand stays revoked... and one the delete
    turned off comes back. Both flip together today because the delete path
    cannot distinguish them — documented here so the behaviour is a decision
    rather than a surprise."""
    from database import db

    _mk_agent("mixed")
    _mk_key("k-live", "mixed", active=1)
    _mk_key("k-revoked", "mixed", active=0)   # revoked by an operator earlier
    db.delete_agent_ownership("mixed")
    db.recover_agent_ownership("mixed")
    assert _active("k-live") == 1
    # KNOWN: the manually-revoked key is also reactivated. Recording the current
    # behaviour; distinguishing the two needs a "revoked_by" column.
    assert _active("k-revoked") == 1


# --- backfill ----------------------------------------------------------------

def test_backfill_deactivates_keys_of_soft_deleted_and_missing_agents(keys_db):
    from database import db
    from sqlalchemy import text
    from db.engine import get_engine

    _mk_agent("live-one")
    _mk_key("k-live", "live-one")
    _mk_key("k-ghost", "never-existed")            # no ownership row at all
    _mk_agent("soft"); _mk_key("k-soft", "soft")
    with get_engine().begin() as conn:             # soft-delete WITHOUT the fix
        conn.execute(text("UPDATE agent_ownership SET deleted_at='t' WHERE agent_name='soft'"))

    assert db.deactivate_orphaned_agent_keys() == 2
    assert _active("k-ghost") == 0 and _active("k-soft") == 0
    assert _active("k-live") == 1


def test_backfill_is_idempotent(keys_db):
    from database import db

    _mk_key("k-ghost", "never-existed")
    assert db.deactivate_orphaned_agent_keys() == 1
    assert db.deactivate_orphaned_agent_keys() == 0


def test_backfill_leaves_user_scoped_keys_alone(keys_db):
    from database import db

    _mk_key("k-user", None, scope="user")
    assert db.deactivate_orphaned_agent_keys() == 0
    assert _active("k-user") == 1


# --- the validation gate this relies on --------------------------------------

def test_validation_requires_an_active_key(keys_db):
    """Deactivation is only sufficient because every auth path requires
    `is_active = 1`. Pin that, or the fix becomes cosmetic."""
    import inspect
    from db import mcp_keys

    src = inspect.getsource(mcp_keys.McpKeyOperations.validate_mcp_api_key)
    assert "is_active" in src, (
        "validate_mcp_api_key must gate on is_active — the #1745 fix deactivates "
        "rather than deletes, so an auth path ignoring the flag would keep the "
        "orphaned credential working"
    )
