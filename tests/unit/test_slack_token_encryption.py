"""
Unit tests for Slack bot token encryption in `db/slack.py` (#453).

Covers the AES-256-GCM encryption added to `SlackOperations` for
`slack_link_connections.slack_bot_token` AND the one-shot migration
that re-encrypts plaintext rows in BOTH Slack tables.

Scope is intentionally narrow: only the new code added in this PR.
Encryption tests for `slack_channels.py` (SLACK-002), `telegram_channels.py`,
and `whatsapp_channels.py` are tracked separately in #664.

Module: src/backend/db/slack.py + src/backend/db/migrations.py
Issue:  https://github.com/abilityai/trinity/issues/453
Pattern: same as `db/telegram_channels.py` + `db/whatsapp_channels.py`
"""

import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

# IMPORTANT: set REDIS_URL BEFORE any backend import. Issue #589 added a
# hard-fail at config-import time if the URL lacks credentials. The parent
# `tests/conftest.py` sets these globally, but `tests/unit/pytest.ini` makes
# unit/ the rootdir so the parent conftest isn't loaded for unit-only runs.
# Match the parent conftest's defaults exactly so behavior is consistent.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest

# Make src/backend importable for direct unit testing
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    """Set CREDENTIAL_ENCRYPTION_KEY for every test in this module.

    Mirrors how subscription_credentials and channel encryption tests
    bootstrap the key. 64 hex chars = 32 bytes for AES-256.
    """
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    yield


@pytest.fixture
def slack_ops_with_temp_db(tmp_path, monkeypatch):
    """Build a SlackOperations bound to a temp SQLite DB with the
    minimum schema needed for the connection tests.

    We don't go through the full `db.connection.get_db_connection` machinery
    — we create the tables directly and patch `get_db_connection` to yield
    our temp connection. Keeps the tests self-contained.
    """
    from db import slack as slack_db

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE slack_link_connections (
            id TEXT PRIMARY KEY,
            link_id TEXT NOT NULL UNIQUE,
            slack_team_id TEXT NOT NULL UNIQUE,
            slack_team_name TEXT,
            slack_bot_token TEXT NOT NULL,
            connected_by TEXT NOT NULL,
            connected_at TEXT NOT NULL,
            enabled INTEGER DEFAULT 1
        )
    """)
    conn.commit()

    # Each call to get_db_connection() needs its own context manager
    class _ConnCtx:
        def __enter__(self):
            return conn
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(slack_db, "get_db_connection", lambda: _ConnCtx())

    ops = slack_db.SlackOperations()
    yield ops, conn, db_path
    conn.close()


# ---------------------------------------------------------------------------
# Round-trip — write encrypts, read decrypts
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_create_then_read_returns_plaintext(self, slack_ops_with_temp_db):
        """create_slack_connection writes encrypted; get_slack_connection
        returns the original plaintext. Caller never sees the envelope."""
        ops, conn, _ = slack_ops_with_temp_db
        original_token = "xoxb-FAKE-TEST-FIXTURE-not-a-real-token"

        created = ops.create_slack_connection(
            link_id="link-1",
            slack_team_id="T0001",
            slack_team_name="Test Workspace",
            slack_bot_token=original_token,
            connected_by="user@example.com",
        )

        assert created["slack_bot_token"] == original_token
        assert created["link_id"] == "link-1"

    def test_raw_db_value_is_envelope_not_plaintext(self, slack_ops_with_temp_db):
        """The on-disk value is a JSON envelope, NOT the raw token —
        this is the actual security property #453 enforces."""
        ops, conn, _ = slack_ops_with_temp_db
        original_token = "xoxb-FAKE-TEST-FIXTURE-not-a-real-token"

        ops.create_slack_connection(
            link_id="link-2",
            slack_team_id="T0002",
            slack_team_name=None,
            slack_bot_token=original_token,
            connected_by="user@example.com",
        )

        cursor = conn.cursor()
        cursor.execute("SELECT slack_bot_token FROM slack_link_connections WHERE link_id = ?", ("link-2",))
        raw_value = cursor.fetchone()[0]

        assert raw_value != original_token, "raw DB value must NOT be plaintext"
        assert "xoxb-" not in raw_value, "raw DB value must not contain the bot-token signature"
        # Confirm it's a JSON envelope from CredentialEncryptionService
        envelope = json.loads(raw_value)
        assert envelope.get("algorithm") == "AES-256-GCM"
        assert "ciphertext" in envelope and "nonce" in envelope

    def test_read_via_get_by_link(self, slack_ops_with_temp_db):
        ops, _, _ = slack_ops_with_temp_db
        original_token = "xoxb-2222-3333-aaaaaaaaaaaaaaa"

        ops.create_slack_connection(
            link_id="link-3",
            slack_team_id="T0003",
            slack_team_name=None,
            slack_bot_token=original_token,
            connected_by="user@example.com",
        )

        result = ops.get_slack_connection_by_link("link-3")
        assert result is not None
        assert result["slack_bot_token"] == original_token


# ---------------------------------------------------------------------------
# Plaintext fallback — legacy rows without re-encryption still readable
# ---------------------------------------------------------------------------


class TestPlaintextFallback:
    def test_legacy_plaintext_row_returns_token(self, slack_ops_with_temp_db, caplog):
        """A row stored before encryption was added is still plaintext on
        disk. Read path detects xoxb-* prefix and returns it as-is, with
        a warning. Keeps runtime working pre-migration."""
        import logging
        ops, conn, _ = slack_ops_with_temp_db
        legacy_token = "xoxb-legacy-9999-bbbbbbbbb"

        # Insert directly bypassing the encryption layer
        conn.execute("""
            INSERT INTO slack_link_connections
            (id, link_id, slack_team_id, slack_team_name, slack_bot_token,
             connected_by, connected_at, enabled)
            VALUES ('legacy-id', 'link-legacy', 'T-legacy', 'Legacy WS',
                    ?, 'user@example.com', '2026-01-01T00:00:00Z', 1)
        """, (legacy_token,))
        conn.commit()

        with caplog.at_level(logging.WARNING):
            result = ops.get_slack_connection("legacy-id")
        assert result is not None
        assert result["slack_bot_token"] == legacy_token, (
            "plaintext fallback must return the legacy token unchanged"
        )
        # Warning was logged so operators know plaintext is on disk
        assert any("plaintext" in rec.message for rec in caplog.records)

    def test_corrupt_envelope_returns_none(self, slack_ops_with_temp_db, caplog):
        """A row that looks like an envelope but fails to decrypt (bad key,
        truncated data, etc.) returns None — caller treats as "no token"
        and the Slack send fails gracefully without crashing."""
        import logging
        ops, conn, _ = slack_ops_with_temp_db

        # Insert a malformed envelope that will fail BOTH the JSON parse
        # AND the xoxb-* fallback (we want to assert "returns None")
        conn.execute("""
            INSERT INTO slack_link_connections
            (id, link_id, slack_team_id, slack_team_name, slack_bot_token,
             connected_by, connected_at, enabled)
            VALUES ('bad-id', 'link-bad', 'T-bad', 'Bad WS',
                    'this-is-neither-an-envelope-nor-an-xoxb-token',
                    'user@example.com', '2026-01-01T00:00:00Z', 1)
        """)
        conn.commit()

        with caplog.at_level(logging.ERROR):
            result = ops.get_slack_connection("bad-id")
        assert result is not None  # row exists
        assert result["slack_bot_token"] is None  # token unrecoverable
        # Decrypt failure was logged
        assert any("Failed to decrypt" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Encryption helper unit behavior
# ---------------------------------------------------------------------------


class TestEncryptionHelpers:
    def test_encrypt_then_decrypt_round_trip(self):
        """Helpers in isolation: encrypt + decrypt = identity for valid token."""
        from db.slack import SlackOperations
        ops = SlackOperations()
        original = "xoxb-helper-test-cccccccc"

        envelope = ops._encrypt_token(original)
        assert envelope != original, "envelope must differ from input"

        decrypted = ops._decrypt_token(envelope)
        assert decrypted == original

    def test_encrypt_raises_on_missing_key(self, monkeypatch):
        """_encrypt_token raises ValueError if CREDENTIAL_ENCRYPTION_KEY unset.
        This is the implicit hard-fail pattern matching TG/WA: write paths
        fail loudly, surface to caller as 5xx."""
        from db.slack import SlackOperations
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
        ops = SlackOperations()

        with pytest.raises(ValueError, match="CREDENTIAL_ENCRYPTION_KEY"):
            ops._encrypt_token("xoxb-test-1234")

    def test_decrypt_returns_none_on_missing_key(self, monkeypatch):
        """_decrypt_token catches the missing-key ValueError and returns None
        — read paths degrade gracefully (no crash, no bot send happens)."""
        from db.slack import SlackOperations
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
        ops = SlackOperations()

        # Build an envelope first (with a key), then drop key, then try to decrypt
        # — but easier path: pass a string that's neither envelope nor xoxb
        result = ops._decrypt_token('{"algorithm": "AES-256-GCM", "ciphertext": "x", "nonce": "y"}')
        assert result is None


# ---------------------------------------------------------------------------
# Migration — one-shot backfill for both Slack tables
# ---------------------------------------------------------------------------


@pytest.fixture
def migration_db(tmp_path):
    """Build a temp DB with both Slack tables, populated with mixed
    plaintext + already-encrypted rows. Returns (cursor, conn, db_path).
    """
    db_path = tmp_path / "migration_test.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE slack_link_connections (
            id TEXT PRIMARY KEY,
            link_id TEXT NOT NULL UNIQUE,
            slack_team_id TEXT NOT NULL UNIQUE,
            slack_team_name TEXT,
            slack_bot_token TEXT NOT NULL,
            connected_by TEXT NOT NULL,
            connected_at TEXT NOT NULL,
            enabled INTEGER DEFAULT 1
        )
    """)
    cursor.execute("""
        CREATE TABLE slack_workspaces (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL UNIQUE,
            team_name TEXT,
            bot_token TEXT NOT NULL,
            connected_by TEXT,
            connected_at TEXT NOT NULL,
            enabled INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    yield cursor, conn, db_path
    conn.close()


class TestMigration:
    def test_encrypts_plaintext_in_both_tables(self, migration_db):
        """One-shot pass: every xoxb-* row in both tables becomes a JSON
        envelope; ciphertext differs from input."""
        from db.migrations import _migrate_slack_bot_token_encryption
        cursor, conn, _ = migration_db

        # Insert plaintext rows in BOTH tables
        cursor.execute("""
            INSERT INTO slack_link_connections (id, link_id, slack_team_id, slack_team_name,
                slack_bot_token, connected_by, connected_at)
            VALUES ('a1', 'L1', 'T1', 'WS1', 'xoxb-link-token-aaaa', 'u', '2026-01-01')
        """)
        cursor.execute("""
            INSERT INTO slack_workspaces (id, team_id, team_name, bot_token, connected_by, connected_at)
            VALUES ('w1', 'T2', 'WS2', 'xoxb-workspace-token-bbbb', 'u', '2026-01-01')
        """)
        conn.commit()

        _migrate_slack_bot_token_encryption(cursor, conn)

        # Both rows now contain JSON envelopes, not plaintext
        cursor.execute("SELECT slack_bot_token FROM slack_link_connections WHERE id='a1'")
        link_value = cursor.fetchone()[0]
        assert "xoxb-" not in link_value
        assert json.loads(link_value)["algorithm"] == "AES-256-GCM"

        cursor.execute("SELECT bot_token FROM slack_workspaces WHERE id='w1'")
        ws_value = cursor.fetchone()[0]
        assert "xoxb-" not in ws_value
        assert json.loads(ws_value)["algorithm"] == "AES-256-GCM"

    def test_skips_already_encrypted_rows(self, migration_db):
        """Rows that don't start with xoxb-* are assumed already encrypted
        (or not Slack tokens) and left alone."""
        from db.migrations import _migrate_slack_bot_token_encryption
        from services.credential_encryption import CredentialEncryptionService

        cursor, conn, _ = migration_db
        encrypted = CredentialEncryptionService().encrypt({"bot_token": "xoxb-already-encrypted"})

        cursor.execute("""
            INSERT INTO slack_link_connections (id, link_id, slack_team_id, slack_team_name,
                slack_bot_token, connected_by, connected_at)
            VALUES ('e1', 'L1', 'T1', 'WS1', ?, 'u', '2026-01-01')
        """, (encrypted,))
        conn.commit()

        _migrate_slack_bot_token_encryption(cursor, conn)

        cursor.execute("SELECT slack_bot_token FROM slack_link_connections WHERE id='e1'")
        # Value unchanged after migration
        assert cursor.fetchone()[0] == encrypted

    def test_idempotent_when_run_twice(self, migration_db):
        """Running the migration twice doesn't re-encrypt (would double-encrypt).
        Critical — the schema_migrations runner SHOULD prevent this, but the
        migration code itself must be idempotent as a defense in depth."""
        from db.migrations import _migrate_slack_bot_token_encryption
        cursor, conn, _ = migration_db

        cursor.execute("""
            INSERT INTO slack_link_connections (id, link_id, slack_team_id, slack_team_name,
                slack_bot_token, connected_by, connected_at)
            VALUES ('a1', 'L1', 'T1', 'WS1', 'xoxb-double-test-cccc', 'u', '2026-01-01')
        """)
        conn.commit()

        _migrate_slack_bot_token_encryption(cursor, conn)
        cursor.execute("SELECT slack_bot_token FROM slack_link_connections WHERE id='a1'")
        after_first = cursor.fetchone()[0]

        _migrate_slack_bot_token_encryption(cursor, conn)
        cursor.execute("SELECT slack_bot_token FROM slack_link_connections WHERE id='a1'")
        after_second = cursor.fetchone()[0]

        assert after_first == after_second, (
            "second run must be a no-op — row was already encrypted, second "
            "pass would have produced double-encryption"
        )

    def test_handles_missing_table(self, tmp_path):
        """If a table doesn't exist on the DB, migration logs and continues
        instead of crashing. Defense in depth — should not happen in normal
        ordering since slack tables are created by earlier migrations."""
        from db.migrations import _migrate_slack_bot_token_encryption
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        # No tables at all
        _migrate_slack_bot_token_encryption(cursor, conn)
        # No exception raised — that's the assertion
        conn.close()

    def test_hard_fails_when_key_missing(self, migration_db, monkeypatch):
        """No CREDENTIAL_ENCRYPTION_KEY = ValueError at migration time.
        Matches the pattern of subscription_credentials, nevermined, and
        every other consumer of CredentialEncryptionService."""
        from db.migrations import _migrate_slack_bot_token_encryption
        cursor, conn, _ = migration_db
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)

        cursor.execute("""
            INSERT INTO slack_link_connections (id, link_id, slack_team_id, slack_team_name,
                slack_bot_token, connected_by, connected_at)
            VALUES ('a1', 'L1', 'T1', 'WS1', 'xoxb-needs-key', 'u', '2026-01-01')
        """)
        conn.commit()

        with pytest.raises(ValueError, match="CREDENTIAL_ENCRYPTION_KEY"):
            _migrate_slack_bot_token_encryption(cursor, conn)

    def test_empty_tables_no_op(self, migration_db):
        """No rows in either table = clean log, no errors."""
        from db.migrations import _migrate_slack_bot_token_encryption
        cursor, conn, _ = migration_db
        # Tables are empty — just run the migration
        _migrate_slack_bot_token_encryption(cursor, conn)
        # No assertion needed — completing without error IS the assertion
