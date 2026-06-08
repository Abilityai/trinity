"""
Unit tests for WhatsApp/Twilio AuthToken encryption in `db/whatsapp_channels.py` (#664).

Backfills coverage for the AES-256-GCM encryption that already shipped on
`whatsapp_bindings.auth_token_encrypted` as part of WHATSAPP-001.

Scope: round-trip via WhatsAppChannelOperations, raw envelope inspection,
decryption-failure handling, account_sid plaintext invariant (the AccountSid
is public Twilio identifier metadata, NOT a credential — only the AuthToken
is encrypted), re-encryption on update, and missing-key behavior.

Module: src/backend/db/whatsapp_channels.py
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
def whatsapp_ops_with_temp_db(tmp_path, monkeypatch):
    """Build a WhatsAppChannelOperations bound to a temp SQLite DB with
    just the schema needed for the binding tests."""
    from db import whatsapp_channels as wa_db

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE whatsapp_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL UNIQUE,
            account_sid TEXT NOT NULL,
            auth_token_encrypted TEXT NOT NULL,
            from_number TEXT NOT NULL,
            messaging_service_sid TEXT,
            display_name TEXT,
            is_sandbox INTEGER DEFAULT 0,
            webhook_secret TEXT NOT NULL UNIQUE,
            webhook_url TEXT,
            enabled INTEGER DEFAULT 1,
            created_by TEXT,
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

    monkeypatch.setattr(wa_db, "get_db_connection", lambda: _ConnCtx())

    ops = wa_db.WhatsAppChannelOperations()
    yield ops, conn, db_path
    conn.close()


# ---------------------------------------------------------------------------
# Round-trip — write encrypts, read decrypts
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_create_then_read_returns_plaintext(self, whatsapp_ops_with_temp_db):
        """create_binding writes encrypted AuthToken; get_decrypted_auth_token
        returns the original plaintext."""
        ops, _, _ = whatsapp_ops_with_temp_db
        original_token = "FAKE-TWILIO-AUTH-TOKEN-not-a-real-secret"

        created = ops.create_binding(
            agent_name="agent-wa-1",
            account_sid="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            auth_token=original_token,
            from_number="whatsapp:+15551234567",
            display_name="Test Sender",
        )

        assert created is not None
        assert created["agent_name"] == "agent-wa-1"
        assert ops.get_decrypted_auth_token("agent-wa-1") == original_token

    def test_raw_db_value_is_envelope_not_plaintext(self, whatsapp_ops_with_temp_db):
        """The on-disk auth_token_encrypted is a JSON envelope, NOT the
        raw AuthToken — the actual security property #664 backfills."""
        ops, conn, _ = whatsapp_ops_with_temp_db
        original_token = "FAKE-TWILIO-AUTH-TOKEN-not-a-real-secret"

        ops.create_binding(
            agent_name="agent-wa-2",
            account_sid="ACyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy",
            auth_token=original_token,
            from_number="whatsapp:+15555550000",
        )

        cursor = conn.cursor()
        cursor.execute(
            "SELECT auth_token_encrypted FROM whatsapp_bindings WHERE agent_name = ?",
            ("agent-wa-2",),
        )
        raw_value = cursor.fetchone()[0]

        assert raw_value != original_token, "raw DB value must NOT be plaintext"
        assert original_token not in raw_value
        envelope = json.loads(raw_value)
        assert envelope.get("algorithm") == "AES-256-GCM"
        assert "ciphertext" in envelope and "nonce" in envelope

    def test_account_sid_stays_plaintext(self, whatsapp_ops_with_temp_db):
        """Twilio-specific invariant: AccountSid is a public identifier
        (appears in URLs, logs, billing dashboards). Only AuthToken needs
        encryption. This pins that explicit distinction so a future refactor
        doesn't accidentally encrypt or strip the SID."""
        ops, conn, _ = whatsapp_ops_with_temp_db
        sid = "ACzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"

        ops.create_binding(
            agent_name="agent-wa-sid",
            account_sid=sid,
            auth_token="some-token-ggggg",
            from_number="whatsapp:+15551112222",
        )

        cursor = conn.cursor()
        cursor.execute(
            "SELECT account_sid FROM whatsapp_bindings WHERE agent_name = ?",
            ("agent-wa-sid",),
        )
        raw_sid = cursor.fetchone()[0]
        assert raw_sid == sid, "AccountSid must be stored verbatim, not encrypted"

        binding = ops.get_binding_by_agent("agent-wa-sid")
        assert binding["account_sid"] == sid
        # And the encrypted column is still an envelope, not the token
        assert binding["auth_token_encrypted"] != "some-token-ggggg"


# ---------------------------------------------------------------------------
# Decryption failure — caller gets None, never an unhandled exception
# ---------------------------------------------------------------------------


class TestDecryptionFailure:
    def test_corrupt_envelope_returns_none(self, whatsapp_ops_with_temp_db, caplog):
        """A row with a malformed envelope causes _decrypt_auth_token to
        swallow the error and return None. No plaintext fallback for
        WhatsApp — invalid bytes simply yield None (a failed Twilio API
        call is the right downstream behavior — opaque-but-safe)."""
        import logging
        ops, conn, _ = whatsapp_ops_with_temp_db
        conn.execute("""
            INSERT INTO whatsapp_bindings
            (agent_name, account_sid, auth_token_encrypted, from_number,
             webhook_secret, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            "agent-wa-bad",
            "ACbadbadbadbadbadbadbadbadbadbadba",
            "this-is-not-an-envelope-and-not-a-token",
            "whatsapp:+15559999999",
            "ws-secret-bad",
            "2026-01-01T00:00:00Z",
        ))
        conn.commit()

        with caplog.at_level(logging.ERROR):
            result = ops.get_decrypted_auth_token("agent-wa-bad")
        assert result is None
        assert any(
            "Failed to decrypt Twilio AuthToken" in rec.message
            for rec in caplog.records
        )

    def test_envelope_with_wrong_key_returns_none(self, whatsapp_ops_with_temp_db, monkeypatch):
        """Encrypt with key A, rotate to key B, read → None.
        The AES-GCM auth tag rejects the wrong key cleanly."""
        ops, _, _ = whatsapp_ops_with_temp_db
        ops.create_binding(
            agent_name="agent-wa-wrong",
            account_sid="ACwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwww",
            auth_token="orig-token-hhhhh",
            from_number="whatsapp:+15558887777",
        )

        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))

        assert ops.get_decrypted_auth_token("agent-wa-wrong") is None


# ---------------------------------------------------------------------------
# Re-encryption on update — same plaintext, different ciphertext
# ---------------------------------------------------------------------------


class TestReEncryptOnUpdate:
    def test_update_produces_fresh_ciphertext(self, whatsapp_ops_with_temp_db):
        """create_binding twice for same agent → ON CONFLICT UPDATE fires.
        AES-GCM's random nonce means a re-encrypted same-plaintext yields a
        different envelope — proves the write path actually re-encrypted."""
        ops, conn, _ = whatsapp_ops_with_temp_db
        token = "same-token-iiiii"

        ops.create_binding(
            agent_name="agent-wa-rot",
            account_sid="ACrotateaaaaaaaaaaaaaaaaaaaaaaaa",
            auth_token=token,
            from_number="whatsapp:+15554443333",
        )
        cursor = conn.cursor()
        cursor.execute(
            "SELECT auth_token_encrypted FROM whatsapp_bindings WHERE agent_name = ?",
            ("agent-wa-rot",),
        )
        first_envelope = cursor.fetchone()[0]

        ops.create_binding(
            agent_name="agent-wa-rot",
            account_sid="ACrotateaaaaaaaaaaaaaaaaaaaaaaaa",
            auth_token=token,
            from_number="whatsapp:+15554443333",
        )
        cursor.execute(
            "SELECT auth_token_encrypted FROM whatsapp_bindings WHERE agent_name = ?",
            ("agent-wa-rot",),
        )
        second_envelope = cursor.fetchone()[0]

        assert first_envelope != second_envelope, (
            "re-create must re-encrypt with a fresh nonce"
        )
        assert ops.get_decrypted_auth_token("agent-wa-rot") == token


# ---------------------------------------------------------------------------
# Encryption helper unit behavior
# ---------------------------------------------------------------------------


class TestEncryptionHelpers:
    def test_encrypt_then_decrypt_round_trip(self):
        """Helpers in isolation: encrypt + decrypt = identity for valid token."""
        from db.whatsapp_channels import WhatsAppChannelOperations
        ops = WhatsAppChannelOperations()
        original = "helper-roundtrip-jjjjj"

        envelope = ops._encrypt_auth_token(original)
        assert envelope != original

        decrypted = ops._decrypt_auth_token(envelope)
        assert decrypted == original

    def test_encrypt_raises_on_missing_key(self, monkeypatch):
        """_encrypt_auth_token has no try/except — missing key surfaces as
        ValueError so the caller (router) can return 5xx."""
        from db.whatsapp_channels import WhatsAppChannelOperations
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
        ops = WhatsAppChannelOperations()

        with pytest.raises(ValueError, match="CREDENTIAL_ENCRYPTION_KEY"):
            ops._encrypt_auth_token("will-not-encrypt")

    def test_decrypt_returns_none_on_missing_key(self, monkeypatch):
        """_decrypt_auth_token catches ValueError → None. The Twilio send
        path treats None as "no credentials" and refuses to call the API."""
        from db.whatsapp_channels import WhatsAppChannelOperations
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
        ops = WhatsAppChannelOperations()

        result = ops._decrypt_auth_token(
            '{"version": 1, "algorithm": "AES-256-GCM", "ciphertext": "x", "nonce": "y"}'
        )
        assert result is None
