"""
Unit tests for Telegram bot token encryption in `db/telegram_channels.py` (#664).

Backfills coverage for the AES-256-GCM encryption that already shipped on
`telegram_bindings.bot_token_encrypted`. Mirrors the pattern of
`tests/unit/test_slack_token_encryption.py` added in #453.

Scope: round-trip via TelegramChannelOperations, raw envelope inspection,
decryption-failure handling, re-encryption on update, and missing-key
behavior of the helpers.

Module: src/backend/db/telegram_channels.py
Issue:  https://github.com/abilityai/trinity/issues/664
"""

import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path

# IMPORTANT: set REDIS_URL BEFORE any backend import. Issue #589 added a
# hard-fail at config-import time if the URL lacks credentials. Match the
# parent conftest's defaults exactly so behavior is consistent.
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
    """Set CREDENTIAL_ENCRYPTION_KEY for every test.

    64 hex chars = 32 bytes for AES-256. Same bootstrap pattern as the
    Slack token encryption tests.
    """
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    yield


@pytest.fixture
def telegram_ops_with_temp_db(tmp_path, monkeypatch):
    """Build a TelegramChannelOperations bound to a temp SQLite DB with
    just the schema needed for the binding tests.

    Patches `get_db_connection` at the module level so every operation
    in the test runs against this temp DB.
    """
    from db import telegram_channels as tg_db

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE telegram_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL UNIQUE,
            bot_token_encrypted TEXT NOT NULL,
            bot_username TEXT,
            bot_id TEXT UNIQUE,
            webhook_secret TEXT NOT NULL,
            webhook_url TEXT,
            telegram_secret_token TEXT,
            last_update_id INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
    """)
    conn.commit()

    class _ConnCtx:
        def __enter__(self):
            return conn
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(tg_db, "get_db_connection", lambda: _ConnCtx())

    ops = tg_db.TelegramChannelOperations()
    yield ops, conn, db_path
    conn.close()


# ---------------------------------------------------------------------------
# Round-trip — write encrypts, read decrypts
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_create_then_read_returns_plaintext(self, telegram_ops_with_temp_db):
        """create_binding writes encrypted; get_decrypted_bot_token returns
        the original plaintext. Caller never sees the envelope."""
        ops, _, _ = telegram_ops_with_temp_db
        original_token = "123456:FAKE-TEST-FIXTURE-not-a-real-tg-token"

        created = ops.create_binding(
            agent_name="agent-tg-1",
            bot_token=original_token,
            bot_username="test_bot",
            bot_id="987654",
        )

        assert created is not None
        assert created["agent_name"] == "agent-tg-1"
        # get_binding_by_agent returns the encrypted blob; the
        # plaintext-revealing accessor is get_decrypted_bot_token.
        assert ops.get_decrypted_bot_token("agent-tg-1") == original_token

    def test_raw_db_value_is_envelope_not_plaintext(self, telegram_ops_with_temp_db):
        """The on-disk value is a JSON envelope, NOT the raw token —
        this is the actual security property the encryption layer enforces."""
        ops, conn, _ = telegram_ops_with_temp_db
        original_token = "123456:FAKE-TEST-FIXTURE-not-a-real-tg-token"

        ops.create_binding(
            agent_name="agent-tg-2",
            bot_token=original_token,
        )

        cursor = conn.cursor()
        cursor.execute(
            "SELECT bot_token_encrypted FROM telegram_bindings WHERE agent_name = ?",
            ("agent-tg-2",),
        )
        raw_value = cursor.fetchone()[0]

        assert raw_value != original_token, "raw DB value must NOT be plaintext"
        assert original_token not in raw_value, (
            "raw DB value must not contain the token substring"
        )
        envelope = json.loads(raw_value)
        assert envelope.get("algorithm") == "AES-256-GCM"
        assert "ciphertext" in envelope and "nonce" in envelope

    def test_get_binding_returns_encrypted_blob(self, telegram_ops_with_temp_db):
        """get_binding_by_agent returns the encrypted column verbatim —
        plaintext is exposed only via get_decrypted_bot_token."""
        ops, _, _ = telegram_ops_with_temp_db
        original_token = "999:tg-blob-test-aaaaaaaaaa"

        ops.create_binding(agent_name="agent-tg-3", bot_token=original_token)

        binding = ops.get_binding_by_agent("agent-tg-3")
        assert binding is not None
        assert binding["bot_token_encrypted"] != original_token
        # Envelope shape, not raw token
        envelope = json.loads(binding["bot_token_encrypted"])
        assert envelope.get("algorithm") == "AES-256-GCM"


# ---------------------------------------------------------------------------
# Decryption failure — caller gets None, never an unhandled exception
# ---------------------------------------------------------------------------


class TestDecryptionFailure:
    def test_corrupt_envelope_returns_none(self, telegram_ops_with_temp_db, caplog):
        """A row with a malformed envelope (bad key, truncated data, etc.)
        causes _decrypt_token to swallow the error and return None. Unlike
        Slack's slack_channels.py there is NO plaintext fallback for
        Telegram — invalid bytes simply yield None."""
        import logging
        ops, conn, _ = telegram_ops_with_temp_db
        # Insert directly bypassing encryption — neither an envelope nor
        # any kind of valid Telegram token format.
        conn.execute("""
            INSERT INTO telegram_bindings
            (agent_name, bot_token_encrypted, webhook_secret, created_at)
            VALUES (?, ?, ?, ?)
        """, (
            "agent-tg-bad",
            "this-is-not-an-envelope-and-not-a-token",
            "ws-secret",
            "2026-01-01T00:00:00Z",
        ))
        conn.commit()

        with caplog.at_level(logging.ERROR):
            result = ops.get_decrypted_bot_token("agent-tg-bad")
        assert result is None, "tampered envelope must yield None, no fallback"
        assert any(
            "Failed to decrypt Telegram bot token" in rec.message
            for rec in caplog.records
        ), "decrypt failure must be logged for operator visibility"

    def test_envelope_with_wrong_key_returns_none(self, telegram_ops_with_temp_db, monkeypatch):
        """An envelope encrypted with key A and read with key B fails the
        GCM auth tag and surfaces as None — the canonical "wrong key" case."""
        ops, _, _ = telegram_ops_with_temp_db
        original_token = "1:wrong-key-token-bbbbbbb"

        ops.create_binding(agent_name="agent-tg-wrong", bot_token=original_token)

        # Rotate the env to a different key — same length, new bytes.
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))

        assert ops.get_decrypted_bot_token("agent-tg-wrong") is None


# ---------------------------------------------------------------------------
# Re-encryption on update — same plaintext, different ciphertext
# ---------------------------------------------------------------------------


class TestReEncryptOnUpdate:
    def test_update_produces_fresh_ciphertext(self, telegram_ops_with_temp_db):
        """Calling create_binding twice for the same agent triggers the
        ON CONFLICT UPDATE path. AES-GCM uses a random 12-byte nonce, so
        even encrypting the same plaintext twice produces distinct
        envelopes — proves the write path actually re-encrypted instead
        of re-using stale ciphertext."""
        ops, conn, _ = telegram_ops_with_temp_db
        token = "1234:rotation-test-ccccccccc"

        ops.create_binding(agent_name="agent-tg-rot", bot_token=token)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT bot_token_encrypted FROM telegram_bindings WHERE agent_name = ?",
            ("agent-tg-rot",),
        )
        first_envelope = cursor.fetchone()[0]

        ops.create_binding(agent_name="agent-tg-rot", bot_token=token)
        cursor.execute(
            "SELECT bot_token_encrypted FROM telegram_bindings WHERE agent_name = ?",
            ("agent-tg-rot",),
        )
        second_envelope = cursor.fetchone()[0]

        assert first_envelope != second_envelope, (
            "second create_binding must re-encrypt (new nonce); "
            "matching envelopes would mean the UPDATE didn't fire"
        )
        # Both still decrypt to the same plaintext
        assert ops.get_decrypted_bot_token("agent-tg-rot") == token

    def test_update_with_new_token_changes_plaintext(self, telegram_ops_with_temp_db):
        """When the token itself rotates, the decrypted plaintext reflects
        the new token (catches a bug where ON CONFLICT might skip the
        encrypted column)."""
        ops, _, _ = telegram_ops_with_temp_db
        ops.create_binding(agent_name="agent-tg-new", bot_token="1:original-ddddd")
        assert ops.get_decrypted_bot_token("agent-tg-new") == "1:original-ddddd"

        ops.create_binding(agent_name="agent-tg-new", bot_token="2:rotated-eeeee")
        assert ops.get_decrypted_bot_token("agent-tg-new") == "2:rotated-eeeee"


# ---------------------------------------------------------------------------
# Encryption helper unit behavior
# ---------------------------------------------------------------------------


class TestEncryptionHelpers:
    def test_encrypt_then_decrypt_round_trip(self):
        """Helpers in isolation: encrypt + decrypt = identity for valid token."""
        from db.telegram_channels import TelegramChannelOperations
        ops = TelegramChannelOperations()
        original = "123456:helper-roundtrip-fffff"

        envelope = ops._encrypt_token(original)
        assert envelope != original

        decrypted = ops._decrypt_token(envelope)
        assert decrypted == original

    def test_encrypt_raises_on_missing_key(self, monkeypatch):
        """_encrypt_token has no try/except — missing key surfaces as
        ValueError. Write paths fail loudly so the caller can return 5xx."""
        from db.telegram_channels import TelegramChannelOperations
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
        ops = TelegramChannelOperations()

        with pytest.raises(ValueError, match="CREDENTIAL_ENCRYPTION_KEY"):
            ops._encrypt_token("123:will-not-encrypt")

    def test_decrypt_returns_none_on_missing_key(self, monkeypatch):
        """_decrypt_token catches the missing-key ValueError and returns
        None — read paths degrade gracefully (no crash, no bot send happens)."""
        from db.telegram_channels import TelegramChannelOperations
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
        ops = TelegramChannelOperations()

        result = ops._decrypt_token(
            '{"version": 1, "algorithm": "AES-256-GCM", "ciphertext": "x", "nonce": "y"}'
        )
        assert result is None
