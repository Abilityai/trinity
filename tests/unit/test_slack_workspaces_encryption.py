"""
Unit tests for Slack-002 workspace bot token encryption in
`db/slack_channels.py` (#664).

Backfills coverage for the AES-256-GCM encryption that already shipped on
`slack_workspaces.bot_token`. NOTE — this is the multi-agent channel routing
module (SLACK-002). The other Slack module, `db/slack.py` (SLACK-001 public
link integration), is covered separately in
`tests/unit/test_slack_token_encryption.py` (#453).

Both modules use a TEXT column whose contents are an AES-256-GCM JSON
envelope (the column was not renamed for backward compatibility). The read
path falls back to plaintext `xoxb-*` so legacy un-migrated rows still
work — this fallback is part of the public contract and is tested here.

Module: src/backend/db/slack_channels.py
Issue:  https://github.com/abilityai/trinity/issues/664
"""

import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path

# IMPORTANT: set REDIS_URL BEFORE any backend import (Issue #589 hard-fail).
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest

# Make src/backend importable for direct unit testing.
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    """Set CREDENTIAL_ENCRYPTION_KEY for every test. 64 hex chars = 32 bytes."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    yield


@pytest.fixture
def slack_channels_ops_with_temp_db(tmp_path, monkeypatch):
    """Build a SlackChannelOperations bound to a temp SQLite DB with the
    workspace + channel-agents schema needed for these tests."""
    from db import slack_channels as sc_db

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
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
    conn.execute("""
        CREATE TABLE slack_channel_agents (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            slack_channel_id TEXT NOT NULL,
            slack_channel_name TEXT,
            agent_name TEXT NOT NULL,
            is_dm_default INTEGER DEFAULT 0,
            created_by TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(team_id, slack_channel_id)
        )
    """)
    conn.commit()

    class _ConnCtx:
        def __enter__(self):
            return conn
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(sc_db, "get_db_connection", lambda: _ConnCtx())

    ops = sc_db.SlackChannelOperations()
    yield ops, conn, db_path
    conn.close()


# ---------------------------------------------------------------------------
# Round-trip — write encrypts, read decrypts
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_create_workspace_returns_plaintext(self, slack_channels_ops_with_temp_db):
        """create_workspace writes encrypted; get_workspace_by_team returns
        the original plaintext bot token. Caller never sees the envelope."""
        ops, _, _ = slack_channels_ops_with_temp_db
        original_token = "xoxb-FAKE-TEST-FIXTURE-not-a-real-token"

        ops.create_workspace(
            team_id="T0001",
            team_name="Test Workspace",
            bot_token=original_token,
            connected_by="user@example.com",
        )

        result = ops.get_workspace_by_team("T0001")
        assert result is not None
        assert result["bot_token"] == original_token
        assert result["team_name"] == "Test Workspace"

    def test_raw_db_value_is_envelope_not_plaintext(self, slack_channels_ops_with_temp_db):
        """The on-disk value is a JSON envelope, NOT the raw xoxb-* token —
        the actual security property #664 backfills for SLACK-002."""
        ops, conn, _ = slack_channels_ops_with_temp_db
        original_token = "xoxb-FAKE-TEST-FIXTURE-not-a-real-token"

        ops.create_workspace(
            team_id="T0002",
            team_name=None,
            bot_token=original_token,
        )

        cursor = conn.cursor()
        cursor.execute(
            "SELECT bot_token FROM slack_workspaces WHERE team_id = ?",
            ("T0002",),
        )
        raw_value = cursor.fetchone()[0]

        assert raw_value != original_token
        assert "xoxb-" not in raw_value, (
            "raw DB value must not contain the bot-token signature"
        )
        envelope = json.loads(raw_value)
        assert envelope.get("algorithm") == "AES-256-GCM"
        assert "ciphertext" in envelope and "nonce" in envelope

    def test_get_workspace_bot_token_returns_plaintext(self, slack_channels_ops_with_temp_db):
        """The convenience accessor get_workspace_bot_token also decrypts."""
        ops, _, _ = slack_channels_ops_with_temp_db
        original_token = "xoxb-accessor-test-aaaa"

        ops.create_workspace(team_id="T0003", team_name=None, bot_token=original_token)

        assert ops.get_workspace_bot_token("T0003") == original_token

    def test_get_all_workspaces_decrypts_each_row(self, slack_channels_ops_with_temp_db):
        """get_all_workspaces decrypts every row, not just the first one."""
        ops, _, _ = slack_channels_ops_with_temp_db
        ops.create_workspace(team_id="T0004", team_name=None, bot_token="xoxb-aaa-bbb")
        ops.create_workspace(team_id="T0005", team_name=None, bot_token="xoxb-ccc-ddd")

        workspaces = ops.get_all_workspaces()
        tokens = {ws["team_id"]: ws["bot_token"] for ws in workspaces}
        assert tokens == {"T0004": "xoxb-aaa-bbb", "T0005": "xoxb-ccc-ddd"}


# ---------------------------------------------------------------------------
# Plaintext fallback — legacy un-migrated rows still readable (lines 47-49)
# ---------------------------------------------------------------------------


class TestPlaintextFallback:
    def test_legacy_plaintext_row_returns_token(self, slack_channels_ops_with_temp_db, caplog):
        """A row inserted before encryption was added stores the raw
        `xoxb-*` token. _decrypt_token's exception path detects the prefix
        and returns the token as-is — keeps runtime working for legacy
        installs that haven't run the re-encryption migration yet.

        Pins `slack_channels.py:47-49` (the explicit fallback)."""
        import logging
        ops, conn, _ = slack_channels_ops_with_temp_db
        legacy_token = "xoxb-legacy-9999-eeeeeeee"

        # Insert directly, bypassing the encryption write path.
        conn.execute("""
            INSERT INTO slack_workspaces
            (id, team_id, team_name, bot_token, connected_by, connected_at, enabled)
            VALUES ('legacy-id', 'T-legacy', 'Legacy WS', ?,
                    'user@example.com', '2026-01-01T00:00:00Z', 1)
        """, (legacy_token,))
        conn.commit()

        with caplog.at_level(logging.WARNING):
            result = ops.get_workspace_by_team("T-legacy")
        assert result is not None
        assert result["bot_token"] == legacy_token, (
            "plaintext fallback must return the legacy token unchanged"
        )

    def test_legacy_plaintext_row_warns_operator(self, slack_channels_ops_with_temp_db, caplog):
        """The plaintext-fallback path emits a WARNING — operators should
        see "this token is on disk in plaintext, re-encrypt soon" when
        plaintext rows are still hanging around."""
        import logging
        ops, conn, _ = slack_channels_ops_with_temp_db
        conn.execute("""
            INSERT INTO slack_workspaces
            (id, team_id, team_name, bot_token, connected_by, connected_at, enabled)
            VALUES ('legacy-warn', 'T-warn', 'Legacy WS', 'xoxb-warn-test-ffff',
                    'user@example.com', '2026-01-01T00:00:00Z', 1)
        """)
        conn.commit()

        with caplog.at_level(logging.WARNING):
            ops.get_workspace_by_team("T-warn")

        assert any(
            "plaintext" in rec.message.lower()
            for rec in caplog.records
            if rec.levelno >= logging.WARNING
        ), "operator-facing plaintext warning must be logged"

    def test_corrupt_non_xoxb_row_returns_empty_token(self, slack_channels_ops_with_temp_db, caplog):
        """A row that's neither an envelope nor an `xoxb-*` prefix can't
        be recovered. _decrypt_token returns None; get_workspace_by_team
        normalizes None to "" so the dict shape stays consistent for
        downstream code. Decrypt failure must still be logged at ERROR."""
        import logging
        ops, conn, _ = slack_channels_ops_with_temp_db
        conn.execute("""
            INSERT INTO slack_workspaces
            (id, team_id, team_name, bot_token, connected_by, connected_at, enabled)
            VALUES ('bad-id', 'T-bad', 'Bad WS',
                    'this-is-neither-an-envelope-nor-an-xoxb-token',
                    'user@example.com', '2026-01-01T00:00:00Z', 1)
        """)
        conn.commit()

        with caplog.at_level(logging.ERROR):
            result = ops.get_workspace_by_team("T-bad")
        assert result is not None
        assert result["bot_token"] == "", (
            "unrecoverable token normalizes to '' (not None) — see line 98"
        )
        assert any(
            "Failed to decrypt" in rec.message for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# Decryption failure — wrong-key path
# ---------------------------------------------------------------------------


class TestDecryptionFailure:
    def test_envelope_with_wrong_key_returns_empty(self, slack_channels_ops_with_temp_db, monkeypatch):
        """Encrypt with key A, rotate to key B, read → ''.
        The AES-GCM auth tag rejects the wrong key cleanly; the workspace
        accessor normalizes the None to ''."""
        ops, _, _ = slack_channels_ops_with_temp_db
        ops.create_workspace(
            team_id="T-wrong",
            team_name=None,
            bot_token="xoxb-wrong-key-test-gggg",
        )

        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))

        result = ops.get_workspace_by_team("T-wrong")
        # Row still exists, but token is unrecoverable
        assert result is not None
        assert result["bot_token"] == ""


# ---------------------------------------------------------------------------
# Re-encryption on update — same plaintext, different ciphertext
# ---------------------------------------------------------------------------


class TestReEncryptOnUpdate:
    def test_update_produces_fresh_ciphertext(self, slack_channels_ops_with_temp_db):
        """Calling create_workspace twice for the same team_id triggers the
        ON CONFLICT UPDATE path. AES-GCM's random nonce means same plaintext
        produces a fresh envelope — proves the write path re-encrypted."""
        ops, conn, _ = slack_channels_ops_with_temp_db
        token = "xoxb-rotation-test-hhhh"

        ops.create_workspace(team_id="T-rot", team_name=None, bot_token=token)
        cursor = conn.cursor()
        cursor.execute("SELECT bot_token FROM slack_workspaces WHERE team_id = ?", ("T-rot",))
        first_envelope = cursor.fetchone()[0]

        ops.create_workspace(team_id="T-rot", team_name=None, bot_token=token)
        cursor.execute("SELECT bot_token FROM slack_workspaces WHERE team_id = ?", ("T-rot",))
        second_envelope = cursor.fetchone()[0]

        assert first_envelope != second_envelope
        assert ops.get_workspace_bot_token("T-rot") == token


# ---------------------------------------------------------------------------
# Encryption helper unit behavior
# ---------------------------------------------------------------------------


class TestEncryptionHelpers:
    def test_encrypt_then_decrypt_round_trip(self):
        """Helpers in isolation: encrypt + decrypt = identity for valid token."""
        from db.slack_channels import SlackChannelOperations
        ops = SlackChannelOperations()
        original = "xoxb-helper-roundtrip-iiii"

        envelope = ops._encrypt_token(original)
        assert envelope != original

        decrypted = ops._decrypt_token(envelope)
        assert decrypted == original

    def test_encrypt_raises_on_missing_key(self, monkeypatch):
        """_encrypt_token has no try/except — missing key surfaces as
        ValueError. Write paths fail loudly."""
        from db.slack_channels import SlackChannelOperations
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
        ops = SlackChannelOperations()

        with pytest.raises(ValueError, match="CREDENTIAL_ENCRYPTION_KEY"):
            ops._encrypt_token("xoxb-no-key-jjjj")

    def test_decrypt_returns_none_on_missing_key_non_xoxb(self, monkeypatch):
        """_decrypt_token catches the missing-key ValueError. For a value
        that doesn't match the xoxb-* fallback prefix it returns None —
        read paths degrade gracefully without crashing."""
        from db.slack_channels import SlackChannelOperations
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
        ops = SlackChannelOperations()

        result = ops._decrypt_token(
            '{"version": 1, "algorithm": "AES-256-GCM", "ciphertext": "x", "nonce": "y"}'
        )
        assert result is None
