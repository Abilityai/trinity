"""
Regression test for #417 — can_agent_message_email crashes on NULL owner email.

Pre-fix: `owner_user.get("email", "").lower()` raised
`AttributeError: 'NoneType' object has no attribute 'lower'` when the owner
user's email column was NULL (admin user on fresh installs).

Post-fix: coerces None → "" via `(... or "")` before `.lower()`.
"""

import os
import sys
import tempfile
import sqlite3
import types
from unittest.mock import MagicMock

import pytest

# Resolve backend path whether the test runs from the repo
# (repo/tests/*.py → repo/src/backend) or inside a container where /app is the
# backend root.
_candidates = [
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")),
    "/app",
]
_backend_path = next((p for p in _candidates if os.path.isdir(os.path.join(p, "db", "agent_settings"))), None)
assert _backend_path, "Could not locate backend path containing db/agent_settings"
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Override the parent conftest's autouse ``cleanup_after_test``, which
    depends on the session-scoped ``api_client`` fixture and therefore needs a
    live backend (``POST /api/token``). These are pure-unit tests against the
    SQLAlchemy engine seam — no backend, no Docker — so the parent's resource
    cleanup is a no-op here. Mirrors ``tests/unit/conftest.py``."""
    yield


@pytest.fixture
def sharing_mixin(monkeypatch):
    """Isolate SharingMixin with an in-memory agent_sharing table."""
    db_file = tempfile.NamedTemporaryFile(suffix="_share_test.db", delete=False)
    db_file.close()
    db_path = db_file.name

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
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    conn.close()

    class _Ctx:
        def __enter__(self):
            self.c = sqlite3.connect(db_path)
            self.c.row_factory = sqlite3.Row
            return self.c

        def __exit__(self, *a):
            self.c.close()

    # Lightweight parent-package stubs so the real db.engine / db.tables and
    # sharing.py's relative imports resolve WITHOUT executing the heavy
    # db/__init__.py. They carry only __path__/__package__. Registered before
    # `import db.engine` so that import doesn't trigger the real package init.
    db_pkg = sys.modules.get("db")
    if db_pkg is None or not hasattr(db_pkg, "__path__"):
        db_pkg = types.ModuleType("db")
        db_pkg.__path__ = [os.path.join(_backend_path, "db")]
        db_pkg.__package__ = "db"
        monkeypatch.setitem(sys.modules, "db", db_pkg)
    ags_pkg = types.ModuleType("db.agent_settings")
    ags_pkg.__path__ = [os.path.join(_backend_path, "db", "agent_settings")]
    ags_pkg.__package__ = "db.agent_settings"
    monkeypatch.setitem(sys.modules, "db.agent_settings", ags_pkg)

    # Legacy raw-sqlite seam: still stubbed for any not-yet-converted module
    # that imports `db.connection`. SharingMixin itself is converted to the
    # SQLAlchemy engine seam (#300), so the real routing happens below.
    fake_conn_mod = types.ModuleType("db.connection")
    fake_conn_mod.get_db_connection = lambda: _Ctx()
    monkeypatch.setitem(sys.modules, "db.connection", fake_conn_mod)

    # SQLAlchemy engine seam (#300): SharingMixin now calls get_engine(), which
    # resolves DATABASE_URL. Point it at the SAME temp file the schema was built
    # on and dispose the per-URL engine cache so the temp file's engine is the
    # one created (and disposed again at teardown).
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    import db.engine as engine_mod
    engine_mod.dispose_engines()

    # Stub db_models with permissive getattr — sharing.py and any transitive
    # db/*.py imports will resolve symbols to MagicMock classes.
    class _PermissiveModule(types.ModuleType):
        def __getattr__(self, name):
            return MagicMock

    fake_models_mod = _PermissiveModule("db_models")
    monkeypatch.setitem(sys.modules, "db_models", fake_models_mod)

    # Direct file-load (bypassing db/__init__.py) under the real dotted name so
    # sharing.py's relative imports (`from ..engine`, `from ..tables`) resolve
    # against the package stubs registered above.
    import importlib.util
    sharing_path = os.path.join(_backend_path, "db", "agent_settings", "sharing.py")
    spec = importlib.util.spec_from_file_location("db.agent_settings.sharing", sharing_path)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "db.agent_settings.sharing", mod)
    spec.loader.exec_module(mod)
    SharingMixin = mod.SharingMixin

    class _SharingOps(SharingMixin):
        """Concrete harness for the mixin."""

        def __init__(self):
            self._user_ops = MagicMock()
            self._owner = None

        def get_agent_owner(self, agent_name):
            return self._owner

    yield _SharingOps(), db_path
    engine_mod.dispose_engines()
    os.unlink(db_path)


def test_null_owner_email_does_not_crash(sharing_mixin):
    """NULL owner.email must not raise; must cleanly return False for non-owner."""
    ops, _ = sharing_mixin
    ops._owner = {"owner_username": "admin"}
    ops._user_ops.get_user_by_username.return_value = {
        "username": "admin",
        "email": None,  # NULL column
    }

    # Must not raise AttributeError.
    result = ops.can_agent_message_email("some-agent", "other@example.com")
    assert result is False


def test_owner_email_match_returns_true(sharing_mixin):
    """Sanity check: real email match still works after the fix."""
    ops, _ = sharing_mixin
    ops._owner = {"owner_username": "alice"}
    ops._user_ops.get_user_by_username.return_value = {
        "username": "alice",
        "email": "Alice@Example.com",
    }

    assert ops.can_agent_message_email("x", "alice@example.com") is True


def test_missing_email_key_returns_false(sharing_mixin):
    """dict without an 'email' key at all (defensive) must not crash."""
    ops, _ = sharing_mixin
    ops._owner = {"owner_username": "admin"}
    ops._user_ops.get_user_by_username.return_value = {"username": "admin"}

    assert ops.can_agent_message_email("x", "someone@example.com") is False


def test_empty_recipient_email_returns_false(sharing_mixin):
    ops, _ = sharing_mixin
    assert ops.can_agent_message_email("x", "") is False


def test_share_with_proactive_flag_admits_recipient(sharing_mixin):
    """The allow_proactive path continues to work when owner email branch is False."""
    ops, db_path = sharing_mixin
    ops._owner = {"owner_username": "admin"}
    ops._user_ops.get_user_by_username.return_value = {
        "username": "admin",
        "email": None,
    }

    # Insert a share with allow_proactive=1.
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO agent_sharing
          (agent_name, shared_with_email, shared_by_id, allow_proactive, created_at)
        VALUES (?, ?, ?, 1, datetime('now'))
        """,
        ("shared-agent", "friend@example.com", "1"),
    )
    conn.commit()
    conn.close()

    assert ops.can_agent_message_email("shared-agent", "friend@example.com") is True
    assert ops.can_agent_message_email("shared-agent", "stranger@example.com") is False
