"""Portal inbox directories must be injective over client emails (ent#308).

The inbox directory is the ONLY thing separating one client's files from
another's. The original name was built by replacing every character outside
`[a-z0-9._-]` with a single `_`, and `@ + ! # $ % & ' * / = ? ^ ` { | } ~` are
all legal in an email local part and all collapse to the same byte. So
`victim+x@example.com` and `victim_x@example.com` shared one directory.

Found by black-box testing a live instance. Confirmed there, end to end: the
second client listed the first's upload, then overwrote its contents, and only
one directory existed on disk.

Impact is not limited to metadata, which is what made it worth a P1. The chat
path feeds inbox contents to the model — `_collect_inbox_images` reads image
bytes for the turn, and the document manifest tells the agent to "read any that
are relevant" — so a colliding client can have the agent read out the other
client's files.

What is pinned here:
  * distinct emails never share a directory, including every collapsing char
  * the same email is still stable (a hash that moved per call would orphan
    every existing inbox on each request)
  * a legacy directory migrates once, in-container, and only when exactly one
    client claims it
  * a legacy directory with TWO claimants is never auto-migrated — the files
    carry no owner, so handing them to either client is the same disclosure
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def portal_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-308.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import metadata as m, agent_sharing, agent_ownership, users
    m.create_all(get_engine(), tables=[agent_sharing, agent_ownership, users])

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(
            id=1, username="admin", role="admin", email="admin@example.com",
            created_at="t", updated_at="t"))
        conn.execute(insert(agent_ownership).values(
            agent_name="atlas", owner_id=1, created_at="t", is_system=0, deleted_at=None))
    yield get_engine()


def _share(engine, agent: str, email: str) -> None:
    from sqlalchemy import insert
    from db.tables import agent_sharing
    with engine.begin() as conn:
        conn.execute(insert(agent_sharing).values(
            agent_name=agent, shared_with_email=email, shared_by_id=1, created_at="t"))


# ---------------------------------------------------------------------------
# The naming itself
# ---------------------------------------------------------------------------

# Every character that the legacy mapping collapsed to `_`. Each pair is a real
# collision that shipped; parametrised so a future "simplification" of the slug
# cannot quietly reintroduce one of them.
_COLLIDING_PAIRS = [
    ("victim+x@example.com", "victim_x@example.com"),
    ("a!b@example.com", "a_b@example.com"),
    ("x&y@example.com", "x_y@example.com"),
    ("p'q@example.com", "p_q@example.com"),
    ("m*n@example.com", "m_n@example.com"),
    ("u=v@example.com", "u_v@example.com"),
    ("s?t@example.com", "s_t@example.com"),
    ("c#d@example.com", "c_d@example.com"),
    ("e$f@example.com", "e_f@example.com"),
    ("g%h@example.com", "g_h@example.com"),
    ("i^j@example.com", "i_j@example.com"),
    ("k{l@example.com", "k_l@example.com"),
    ("w|z@example.com", "w_z@example.com"),
    ("y~a@example.com", "y_a@example.com"),
]


@pytest.mark.parametrize("a,b", _COLLIDING_PAIRS)
def test_distinct_emails_never_share_a_directory(a, b):
    from client_portal.service import _safe_email_dir, _legacy_email_dir

    assert _legacy_email_dir(a) == _legacy_email_dir(b), (
        "precondition: this pair must be a real legacy collision, else the case proves nothing"
    )
    assert _safe_email_dir(a) != _safe_email_dir(b)


def test_the_same_email_always_maps_to_the_same_directory():
    """Stability is not cosmetic: a name that varied per call would orphan the
    client's existing inbox on every single request."""
    from client_portal.service import _safe_email_dir

    assert _safe_email_dir("bob@example.com") == _safe_email_dir("bob@example.com")
    # Case and surrounding whitespace are the same person, not a new inbox.
    assert _safe_email_dir("  BOB@Example.COM ") == _safe_email_dir("bob@example.com")


def test_the_directory_stays_readable():
    """The agent and the operator both read these paths — a bare hash would be a
    regression in a different direction."""
    from client_portal.service import _safe_email_dir

    d = _safe_email_dir("bob@example.com")
    assert d.startswith("bob_example.com-"), d
    assert "/" not in d and ".." not in d


def test_directory_name_is_still_path_safe():
    from client_portal.service import _safe_email_dir

    for hostile in ("../../etc/passwd@x.com", "a/b@x.com", "..@x.com", ".hidden@x.com", ""):
        d = _safe_email_dir(hostile)
        assert "/" not in d, d
        assert d not in (".", ".."), d
        # No dotfile directories: an inbox the agent's `ls` cannot see is an
        # inbox the operator will be told does not exist.
        assert not d.startswith("."), d


# ---------------------------------------------------------------------------
# Migrating a pre-fix inbox
# ---------------------------------------------------------------------------

def test_a_single_claimant_inbox_is_migrated(portal_db):
    from client_portal import service

    _share(portal_db, "atlas", "solo@example.com")
    assert service._legacy_migration_is_safe("atlas", "solo@example.com") is True


def test_a_two_claimant_inbox_is_never_migrated(portal_db, monkeypatch):
    """The files carry no owner. Renaming the directory to either client's new
    name hands them the other's files — the exact disclosure this fix exists to
    stop — so it must refuse and escalate instead."""
    from client_portal import service

    _share(portal_db, "atlas", "victim+x@example.com")
    _share(portal_db, "atlas", "victim_x@example.com")

    alerts = []
    monkeypatch.setattr(service, "_alert_collided_inbox",
                        lambda a, d, c: alerts.append((a, d, tuple(c))))

    for email in ("victim+x@example.com", "victim_x@example.com"):
        assert service._legacy_migration_is_safe("atlas", email) is False

    assert alerts, "a refusal the operator never hears about is a silent data problem"
    agent, legacy, claimants = alerts[0]
    assert agent == "atlas"
    assert legacy == "victim_x_example.com"
    assert claimants == ("victim+x@example.com", "victim_x@example.com")


def test_migration_check_fails_closed_on_a_db_error(portal_db, monkeypatch):
    """An un-migrated inbox is visible and recoverable; a misattributed one is
    not. So an error must mean 'do not move it'."""
    from client_portal import db as portal_db_mod
    from client_portal import service

    def _boom(agent_name):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(portal_db_mod, "list_agent_share_emails", _boom)
    assert service._legacy_migration_is_safe("atlas", "solo@example.com") is False


def test_migration_is_conditional_inside_the_container(portal_db):
    """The rename is decided in the container, not by a read-then-write from the
    backend: two concurrent requests would otherwise both see 'legacy exists' and
    race. Folded into the listing script so it also costs no extra docker exec.
    """
    from client_portal.service import (
        _client_inbox, _inbox_list_cmd, _legacy_client_inbox,
    )
    import base64 as _b64

    cmd = _inbox_list_cmd(_client_inbox("bob@example.com"), _legacy_client_inbox("bob@example.com"))
    script = _b64.b64decode(cmd.split("echo ")[1].split(" |")[0]).decode()

    assert "os.rename(legacy,d)" in script
    assert "not os.path.exists(d)" in script, "must not clobber an existing new inbox"
    assert "os.path.isdir(legacy)" in script
    assert "except OSError" in script, "a failed rename must degrade to an empty listing, not crash"


def test_no_legacy_dir_is_passed_when_migration_is_unsafe(portal_db, monkeypatch):
    """The refusal has to reach the container command, not just the log."""
    from client_portal.service import _inbox_list_cmd, _client_inbox
    import base64 as _b64

    cmd = _inbox_list_cmd(_client_inbox("bob@example.com"), None)
    script = _b64.b64decode(cmd.split("echo ")[1].split(" |")[0]).decode()

    assert "legacy=None" in script
    assert "if legacy and" in script, "a None legacy must short-circuit the rename"


def test_writes_always_target_the_new_directory():
    """Reads may consult the legacy path during migration; writes never may, or
    the collision persists for every client that never opened their inbox."""
    from client_portal.service import _client_inbox, _safe_email_dir

    inbox = _client_inbox("victim+x@example.com")
    assert inbox.endswith(_safe_email_dir("victim+x@example.com"))
    assert inbox != _client_inbox("victim_x@example.com")
