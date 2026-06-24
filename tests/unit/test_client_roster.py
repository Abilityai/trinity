"""Unit tests for the external client roster (#20 — Access/Sharing redesign).

Covers:
- DB join methods `list_clients_for_agent` on Telegram + WhatsApp ops: correct
  normalization (channel/identity/display_name/verified_email/message_count/
  last_active) and tenant isolation (only the agent's own clients).
- `client_roster_service.get_client_roster`: cross-channel aggregation, sort by
  last_active descending with never-active rows last, and per-channel failure
  degradation.

Module: src/backend/services/client_roster_service.py
        src/backend/db/telegram_channels.py
        src/backend/db/whatsapp_channels.py
Issue:  Abilityai/trinity-enterprise#20
"""

import os
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace

# IMPORTANT: set REDIS_URL BEFORE any backend import (Issue #589 hard-fail).
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import db_backend  # noqa: E402,F401


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    """create_binding encrypts the bot/auth token; needs a key."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    yield


# ---------------------------------------------------------------------------
# DB join methods
# ---------------------------------------------------------------------------


class TestTelegramRosterQuery:
    def test_lists_only_this_agents_clients_with_normalization(self, db_backend):
        from db import telegram_channels as tg_db
        ops = tg_db.TelegramChannelOperations()

        ops.create_binding(agent_name="agent-a", bot_token="tok-a", bot_id="111")
        ops.create_binding(agent_name="agent-b", bot_token="tok-b", bot_id="222")
        bind_a = ops.get_binding_by_agent("agent-a")["id"]
        bind_b = ops.get_binding_by_agent("agent-b")["id"]

        # agent-a: one with username, one without
        ops.get_or_create_chat_link(bind_a, "1001", "alice")
        ops.get_or_create_chat_link(bind_a, "1002", None)
        ops.set_verified_email(bind_a, "1001", "alice@example.com")
        # agent-b: should never appear in agent-a's roster
        ops.get_or_create_chat_link(bind_b, "2001", "bob")

        clients = ops.list_clients_for_agent("agent-a")
        assert len(clients) == 2
        by_identity = {c["identity"]: c for c in clients}

        assert "@alice" in by_identity
        assert by_identity["@alice"]["channel"] == "telegram"
        assert by_identity["@alice"]["display_name"] == "alice"
        assert by_identity["@alice"]["verified_email"] == "alice@example.com"

        # No username → identity falls back to the numeric user id, email None
        assert "1002" in by_identity
        assert by_identity["1002"]["display_name"] is None
        assert by_identity["1002"]["verified_email"] is None

    def test_empty_for_agent_without_binding(self, db_backend):
        from db import telegram_channels as tg_db
        assert tg_db.TelegramChannelOperations().list_clients_for_agent("nope") == []


class TestWhatsAppRosterQuery:
    def test_lists_clients_with_phone_identity(self, db_backend):
        from db import whatsapp_channels as wa_db
        ops = wa_db.WhatsAppChannelOperations()

        ops.create_binding(
            agent_name="agent-wa",
            account_sid="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            auth_token="tok",
            from_number="whatsapp:+15551230000",
        )
        bind = ops.get_binding_by_agent("agent-wa")["id"]
        ops.get_or_create_chat_link(bind, "whatsapp:+15559998888", "Carol")
        ops.set_verified_email(bind, "whatsapp:+15559998888", "carol@example.com")

        clients = ops.list_clients_for_agent("agent-wa")
        assert len(clients) == 1
        c = clients[0]
        assert c["channel"] == "whatsapp"
        assert c["identity"] == "whatsapp:+15559998888"
        assert c["display_name"] == "Carol"
        assert c["verified_email"] == "carol@example.com"


# ---------------------------------------------------------------------------
# Service aggregation + sort
# ---------------------------------------------------------------------------


def _entry(channel, identity, last_active, count=0):
    return {
        "channel": channel,
        "identity": identity,
        "display_name": None,
        "verified_email": None,
        "message_count": count,
        "last_active": last_active,
    }


class TestRosterService:
    def test_merges_channels_sorted_desc_nulls_last(self, monkeypatch):
        import services.client_roster_service as svc

        tg = [
            _entry("telegram", "@old", "2026-01-01T00:00:00Z"),
            _entry("telegram", "@never", None),
        ]
        wa = [
            _entry("whatsapp", "whatsapp:+1", "2026-06-01T00:00:00Z"),
        ]
        fake_db = SimpleNamespace(
            list_telegram_clients_for_agent=lambda name: tg,
            list_whatsapp_clients_for_agent=lambda name: wa,
        )
        monkeypatch.setattr(svc, "db", fake_db)

        roster = svc.get_client_roster("agent-x")
        identities = [c["identity"] for c in roster]
        # newest first, never-active last
        assert identities == ["whatsapp:+1", "@old", "@never"]

    def test_one_channel_failing_degrades_to_other(self, monkeypatch):
        import services.client_roster_service as svc

        def boom(name):
            raise RuntimeError("telegram source down")

        wa = [_entry("whatsapp", "whatsapp:+1", "2026-06-01T00:00:00Z")]
        fake_db = SimpleNamespace(
            list_telegram_clients_for_agent=boom,
            list_whatsapp_clients_for_agent=lambda name: wa,
        )
        monkeypatch.setattr(svc, "db", fake_db)

        roster = svc.get_client_roster("agent-x")
        assert [c["identity"] for c in roster] == ["whatsapp:+1"]
