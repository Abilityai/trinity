"""
Regression tests for #446 — team-shared users must pass the public-link gate
without being queued into access_requests, regardless of email casing.

These are isolated unit tests over `SharingMixin` (mirrors the test style of
`test_sharing_null_email_unit.py`). They cover:

  1. `email_has_agent_access` returns True for a team-shared email even when
     the gate receives the email in mixed case (casing defense-in-depth).
  2. `share_agent` clears any pre-existing pending access_request row for
     the same (agent, email), so the owner's Pending list doesn't
     double-prompt them after a manual Team Share add.
  3. Sharing a brand-new email with no pending row is still a no-op on the
     access_requests table (no spurious deletes, no crashes).
"""

import os
import sys
import tempfile
import sqlite3
from datetime import datetime
from unittest.mock import MagicMock

import pytest


_candidates = [
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")),
    "/app",
]
_backend_path = next(
    (p for p in _candidates if os.path.isdir(os.path.join(p, "db", "agent_settings"))),
    None,
)
assert _backend_path, "Could not locate backend path containing db/agent_settings"
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)


# ---------------------------------------------------------------------------
# Neutralize the parent conftest's session-scoped autouse `cleanup_after_test`
# fixture, which depends on `api_client` and therefore requires a LIVE backend
# (it authenticates over HTTP). These are pure DB-layer unit tests with no
# backend — shadow both fixtures at module scope so collection doesn't try to
# open an HTTP connection.
# ---------------------------------------------------------------------------
@pytest.fixture
def api_client():
    return None


@pytest.fixture(autouse=True)
def cleanup_after_test():
    yield


@pytest.fixture
def sharing_harness(monkeypatch, tmp_path):
    """SharingMixin (#300 SQLAlchemy seam) wired to a temp-file agent_sharing +
    access_requests DB.

    The converted `db/agent_settings/sharing.py` routes all queries through
    `get_engine()` (which reads `DATABASE_URL`), so the temp DB is selected by
    setenv'ing `DATABASE_URL` at the schema file and disposing the URL-keyed
    engine cache (before ops run AND at teardown). The schema is built on the
    SAME file the engine opens.
    """
    db_path = str(tmp_path / "gate_test.db")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE agent_sharing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            shared_with_email TEXT NOT NULL,
            shared_by_id TEXT NOT NULL,
            shared_by_email TEXT,
            allow_proactive INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(agent_name, shared_with_email)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE access_requests (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            email TEXT NOT NULL,
            channel TEXT,
            requested_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            decided_by INTEGER,
            decided_at TEXT,
            UNIQUE(agent_name, email)
        )
        """
    )
    conn.commit()
    conn.close()

    # Route the SQLAlchemy engine (#300) at the temp file. The engine cache is
    # keyed by URL, so dispose after setting DATABASE_URL so the temp file's
    # engine is the one created.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    import db.engine as engine_mod
    engine_mod.dispose_engines()

    from db.agent_settings.sharing import SharingMixin

    class _SharingOps(SharingMixin):
        def __init__(self):
            self._user_ops = MagicMock()
            self._owner = None

        def get_agent_owner(self, agent_name):
            return self._owner

    yield _SharingOps(), db_path
    engine_mod.dispose_engines()


def _seed_share(db_path: str, agent: str, email_lower: str, sharer_id: str = "1") -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO agent_sharing
          (agent_name, shared_with_email, shared_by_id, created_at)
        VALUES (?, ?, ?, datetime('now'))
        """,
        (agent, email_lower, sharer_id),
    )
    conn.commit()
    conn.close()


def _seed_pending_request(
    db_path: str,
    agent: str,
    email_lower: str,
    channel: str = "web",
    rid: str = "req_1",
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO access_requests
          (id, agent_name, email, channel, requested_at, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
        """,
        (rid, agent, email_lower, channel, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def _count_pending(db_path: str, agent: str, email_lower: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM access_requests WHERE agent_name=? AND email=? AND status='pending'",
            (agent, email_lower),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# email_has_agent_access — gate behavior
# ---------------------------------------------------------------------------


def test_team_shared_email_passes_gate_lowercase(sharing_harness):
    """Baseline: a team-shared email passes `email_has_agent_access`."""
    ops, db_path = sharing_harness
    ops._owner = {"owner_username": "owner"}
    ops._user_ops.get_user_by_email.return_value = None
    _seed_share(db_path, "agent-x", "alice@example.com")

    assert ops.email_has_agent_access("agent-x", "alice@example.com") is True


def test_team_shared_email_passes_gate_mixed_case(sharing_harness):
    """#446: mixed-case session email still matches the lowercase allow-list."""
    ops, db_path = sharing_harness
    ops._owner = {"owner_username": "owner"}
    ops._user_ops.get_user_by_email.return_value = None
    _seed_share(db_path, "agent-x", "alice@example.com")

    # Gate may be invoked with whatever casing the session/channel produced.
    assert ops.email_has_agent_access("agent-x", "Alice@Example.COM") is True
    assert ops.email_has_agent_access("agent-x", "  alice@example.com  ") is True


def test_non_shared_email_still_fails_gate(sharing_harness):
    """Non-shared emails must still return False (no casing leak)."""
    ops, db_path = sharing_harness
    ops._owner = {"owner_username": "owner"}
    ops._user_ops.get_user_by_email.return_value = None
    _seed_share(db_path, "agent-x", "alice@example.com")

    assert ops.email_has_agent_access("agent-x", "bob@example.com") is False


def test_empty_email_returns_false(sharing_harness):
    ops, _ = sharing_harness
    ops._owner = {"owner_username": "owner"}
    assert ops.email_has_agent_access("agent-x", "") is False
    assert ops.email_has_agent_access("agent-x", None) is False
    assert ops.email_has_agent_access("agent-x", "   ") is False


# ---------------------------------------------------------------------------
# share_agent — stale pending cleanup
# ---------------------------------------------------------------------------


def test_share_agent_clears_stale_pending_request(sharing_harness):
    """#446: adding a Team Share drops any pre-existing pending access_request.

    Scenario: user tried to chat before being shared, landing in the queue.
    Owner then adds them to Team Sharing manually. The stale pending row
    must be gone so the Pending list doesn't double-prompt the owner.
    """
    ops, db_path = sharing_harness
    ops._owner = {"owner_username": "owner"}
    ops._user_ops.get_user_by_username.return_value = {
        "id": 1,
        "username": "owner",
        "email": "owner@example.com",
        "role": "user",
    }

    _seed_pending_request(db_path, "agent-x", "alice@example.com")
    assert _count_pending(db_path, "agent-x", "alice@example.com") == 1

    share = ops.share_agent("agent-x", "owner", "Alice@Example.com")
    assert share is not None
    assert share.shared_with_email == "alice@example.com"
    # Stale pending row must be deleted atomically with the share insert.
    assert _count_pending(db_path, "agent-x", "alice@example.com") == 0


def test_share_agent_no_pending_row_is_noop_on_access_requests(sharing_harness):
    """Sharing a fresh email must not crash when there's nothing to delete."""
    ops, db_path = sharing_harness
    ops._owner = {"owner_username": "owner"}
    ops._user_ops.get_user_by_username.return_value = {
        "id": 1,
        "username": "owner",
        "email": "owner@example.com",
        "role": "user",
    }

    share = ops.share_agent("agent-x", "owner", "bob@example.com")
    assert share is not None
    assert _count_pending(db_path, "agent-x", "bob@example.com") == 0


def test_share_agent_does_not_clear_other_agents_pending(sharing_harness):
    """Cleanup scope: only (this agent, this email) pending rows are cleared."""
    ops, db_path = sharing_harness
    ops._owner = {"owner_username": "owner"}
    ops._user_ops.get_user_by_username.return_value = {
        "id": 1,
        "username": "owner",
        "email": "owner@example.com",
        "role": "user",
    }

    _seed_pending_request(db_path, "agent-x", "alice@example.com", rid="rx")
    _seed_pending_request(db_path, "agent-y", "alice@example.com", rid="ry")

    ops.share_agent("agent-x", "owner", "alice@example.com")

    assert _count_pending(db_path, "agent-x", "alice@example.com") == 0
    # Unrelated agent's pending row must remain untouched.
    assert _count_pending(db_path, "agent-y", "alice@example.com") == 1


def test_share_agent_normalizes_whitespace_and_case(sharing_harness):
    """Sharing `  Alice@Example.COM  ` lands as `alice@example.com` and clears pending."""
    ops, db_path = sharing_harness
    ops._owner = {"owner_username": "owner"}
    ops._user_ops.get_user_by_username.return_value = {
        "id": 1,
        "username": "owner",
        "email": "owner@example.com",
        "role": "user",
    }

    _seed_pending_request(db_path, "agent-x", "alice@example.com")

    share = ops.share_agent("agent-x", "owner", "  Alice@Example.COM  ")
    assert share is not None
    assert share.shared_with_email == "alice@example.com"
    assert _count_pending(db_path, "agent-x", "alice@example.com") == 0
