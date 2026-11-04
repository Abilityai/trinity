"""trinity-enterprise#21 — per-client access controls for channel users.

An agent's owner can block, unblock and revoke one external channel client from
the Sharing-tab roster, and approve a pending one. Covered here against a real
database (both backends via the harness):

- the roster carries each client's ``client_id`` and access state —
  blocked / granted / pending (with the request id approve needs) / none;
- block is idempotent, per agent and per channel, and keeps the client's share;
- unblock restores exactly what they had; unblocking an unblocked client is 409;
- revoke removes the share (so a restricted agent asks again), refuses a client
  with nothing to revoke, and works on a blocked client too;
- every control refuses a client that never reached this agent (404).

The gate that enforces a block is in ``test_message_router_access_gate.py``; the
routes' person/owner gate in the route census and below.
"""

import os
import secrets
import sys
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

AGENT, OTHER = "agent-a", "agent-b"
OWNER = "owner"
ALICE = "alice@example.com"   # telegram 1001, shared on AGENT
CAROL = "carol@example.com"   # whatsapp, pending on AGENT
WA = "whatsapp:+15559998888"


@pytest.fixture
def world(db_backend, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    from database import db
    from db_models import UserCreate

    db.create_user(UserCreate(username=OWNER, password="x", role="creator", email="owner@example.com"))
    for name in (AGENT, OTHER):
        db.register_agent_owner(name, OWNER)

    db.create_telegram_binding(agent_name=AGENT, bot_token="tok-a", bot_id="111")
    tg = db.get_telegram_binding(AGENT)["id"]
    db.record_telegram_inbound(tg, "1001", "alice")
    db.set_telegram_verified_email(tg, "1001", ALICE)
    db.record_telegram_inbound(tg, "1002", None)          # never verified

    db.create_whatsapp_binding(agent_name=AGENT, account_sid="AC" + "x" * 32, auth_token="tok",
                               from_number="whatsapp:+15551230000")
    wa = db.get_whatsapp_binding(AGENT)["id"]
    db.record_whatsapp_inbound(wa, WA, "Carol")
    db.set_whatsapp_verified_email(wa, WA, CAROL)

    db.share_agent(AGENT, OWNER, ALICE)
    db.upsert_access_request(AGENT, CAROL, "whatsapp")
    return db


def _roster():
    from services.client_roster_service import get_client_roster
    return {(c["channel"], c["client_id"]): c for c in get_client_roster(AGENT)}


def _block(client=("telegram", "1001"), reason=None, agent=AGENT):
    from services import client_roster_service as svc
    return svc.block_client(agent, *client, actor_id=1, actor_email="owner@example.com", reason=reason)


def _err(fn, *a, **kw):
    from services.client_roster_service import ClientControlError
    with pytest.raises(ClientControlError) as e:
        fn(*a, **kw)
    return e.value.status_code


class TestRosterAccessState:
    def test_each_row_carries_its_client_id_and_state(self, world):
        r = _roster()
        assert set(r) == {("telegram", "1001"), ("telegram", "1002"), ("whatsapp", WA)}
        assert r[("telegram", "1001")]["access"] == "granted"
        assert r[("telegram", "1002")]["access"] == "none"
        carol = r[("whatsapp", WA)]
        assert carol["access"] == "pending"
        pending = world.list_access_requests(AGENT, "pending")
        assert carol["access_request_id"] == pending[0]["id"]

    def test_a_block_wins_and_says_who_and_why(self, world):
        _block(reason="  spam  ")
        alice = _roster()[("telegram", "1001")]
        assert alice["access"] == "blocked"
        assert alice["blocked_by_email"] == "owner@example.com"
        assert alice["block_reason"] == "spam"
        assert alice["blocked_at"]


class TestBlock:
    def test_block_is_what_the_gate_reads_and_is_scoped(self, world):
        _block()
        assert world.is_channel_client_blocked(AGENT, "telegram", "1001")
        # Not the same id on another channel, not another agent, not another client.
        assert not world.is_channel_client_blocked(AGENT, "whatsapp", "1001")
        assert not world.is_channel_client_blocked(OTHER, "telegram", "1001")
        assert not world.is_channel_client_blocked(AGENT, "telegram", "1002")

    def test_block_is_idempotent_and_keeps_the_first_block(self, world):
        first = _block(reason="first")
        again = _block(reason="second")
        assert again["blocked_at"] == first["blocked_at"] and again["reason"] == "first"
        assert len(world.list_channel_client_blocks(AGENT)) == 1

    def test_block_keeps_the_share_and_unblock_restores_it(self, world):
        from services import client_roster_service as svc
        _block()
        assert ALICE in {s.shared_with_email for s in world.get_agent_shares(AGENT)}
        svc.unblock_client(AGENT, "telegram", "1001")
        assert not world.is_channel_client_blocked(AGENT, "telegram", "1001")
        assert _roster()[("telegram", "1001")]["access"] == "granted"

    def test_an_unverified_client_is_blockable(self, world):
        _block(("telegram", "1002"))
        assert world.is_channel_client_blocked(AGENT, "telegram", "1002")

    def test_unblocking_a_client_that_is_not_blocked_is_409(self, world):
        from services import client_roster_service as svc
        assert _err(svc.unblock_client, AGENT, "telegram", "1001") == 409


class TestRevoke:
    def test_revoke_removes_the_share_and_a_restricted_agent_asks_again(self, world):
        from services import client_roster_service as svc
        out = svc.revoke_client(AGENT, "telegram", "1001", actor_username=OWNER)
        assert out["email"] == ALICE
        assert ALICE not in {s.shared_with_email for s in world.get_agent_shares(AGENT)}
        assert not world.email_has_agent_access(AGENT, ALICE)
        assert _roster()[("telegram", "1001")]["access"] == "none"

    def test_a_blocked_client_can_still_be_revoked(self, world):
        from services import client_roster_service as svc
        _block()
        svc.revoke_client(AGENT, "telegram", "1001", actor_username=OWNER)
        assert ALICE not in {s.shared_with_email for s in world.get_agent_shares(AGENT)}
        assert _roster()[("telegram", "1001")]["access"] == "blocked"

    @pytest.mark.parametrize("client", [("telegram", "1002"), ("whatsapp", WA)])
    def test_nothing_to_revoke_is_409(self, world, client):
        from services import client_roster_service as svc
        assert _err(svc.revoke_client, AGENT, *client, actor_username=OWNER) == 409


class TestOnlyThisAgentsClients:
    @pytest.mark.parametrize("client", [("telegram", "9999"), ("whatsapp", "1001"), ("slack", "U1")])
    def test_every_control_refuses_a_client_that_never_reached_this_agent(self, world, client):
        from services import client_roster_service as svc
        assert _err(_block, client) == 404
        assert _err(svc.unblock_client, AGENT, *client) == 404
        assert _err(svc.revoke_client, AGENT, *client, actor_username=OWNER) == 404
        assert world.list_channel_client_blocks(AGENT) == []

    def test_another_agents_client_is_not_found_here(self, world):
        assert _err(_block, ("telegram", "1001"), agent=OTHER) == 404


class TestRoutes:
    """The three routes take an owner and a person, and audit what they did."""

    def test_routes_are_owner_and_person_gated_and_audited(self):
        import inspect
        from routers import sharing
        for fn in (sharing.block_channel_client_endpoint, sharing.unblock_channel_client_endpoint,
                   sharing.revoke_channel_client_endpoint):
            sig = inspect.signature(fn)
            assert "get_owned_agent_by_name" in str(sig.parameters["agent_name"].annotation), fn.__name__
            src = inspect.getsource(fn)
            assert "assert_person(current_user)" in src and "_audit_client_control(" in src, fn.__name__
        routes = {(tuple(sorted(r.methods)), r.path): r for r in sharing.router.routes}
        for action in ("block", "unblock", "revoke"):
            r = routes[(("POST",), f"/api/agents/{{agent_name}}/clients/{action}")]
            assert any(getattr(d.dependency, "__name__", "") == "require_person" for d in r.dependencies)


# ---------------------------------------------------------------------------
# Review of #4177: every way a blocked client could still reach the agent,
# against the real database
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402


def _tg_update(sender="1001", text="hello", chat_type="private", chat_id=None):
    return {"update_id": 1, "message": {
        "message_id": 7, "date": 1700000000, "text": text,
        "from": {"id": int(sender), "is_bot": False, "first_name": "A", "username": "a"},
        "chat": {"id": int(chat_id or sender), "type": chat_type},
    }}


class TestTheSenderIdIsTheRosterClientId:
    def test_a_real_telegram_message_is_blocked_by_the_roster_id(self, world):
        """The gate keys on the parsed `sender_id`, the roster on `client_id`;
        nothing executed that the two are the same string."""
        from adapters.telegram_adapter import TelegramAdapter
        parsed = TelegramAdapter().parse_message({**_tg_update("1001"), "_bot_id": "111"})
        assert parsed is not None
        client_id = _roster()[("telegram", "1001")]["client_id"]
        assert parsed.sender_id == client_id
        _block(("telegram", client_id))
        assert world.is_channel_client_blocked(AGENT, "telegram", parsed.sender_id)


class TestTransportCommands:
    def _telegram(self, world, monkeypatch, text):
        from adapters.transports import telegram_webhook as tw
        transport = tw.TelegramWebhookTransport.__new__(tw.TelegramWebhookTransport)
        adapter = MagicMock()
        from adapters.telegram_adapter import TelegramAdapter
        adapter.parse_message = lambda update: TelegramAdapter().parse_message({**update, "_bot_id": "111"})
        adapter.handle_command = AsyncMock(return_value="Send me your email")
        adapter._send_message = AsyncMock()
        adapter.get_session_identifier = MagicMock(return_value="sid")
        transport.adapter = adapter
        transport.on_event = AsyncMock()
        asyncio.run(transport._process_update(_tg_update("1001", text), world.get_telegram_binding(AGENT)))
        return adapter, transport

    def test_a_blocked_senders_telegram_command_gets_no_reply(self, world, monkeypatch):
        _block(("telegram", "1001"))
        adapter, transport = self._telegram(world, monkeypatch, "/login")
        adapter.handle_command.assert_not_awaited()
        adapter._send_message.assert_not_awaited()
        transport.on_event.assert_not_awaited()

    def test_an_unblocked_senders_telegram_command_is_answered(self, world, monkeypatch):
        adapter, _ = self._telegram(world, monkeypatch, "/login")
        adapter.handle_command.assert_awaited_once()

    def _whatsapp(self, world):
        from adapters.transports import twilio_webhook as tw
        transport = tw.TwilioWebhookTransport.__new__(tw.TwilioWebhookTransport)
        adapter = MagicMock()
        msg = MagicMock(sender_id=WA, channel_id=WA, thread_id=None)
        adapter.parse_message = MagicMock(return_value=msg)
        adapter.is_command = MagicMock(return_value=True)
        adapter.handle_command = AsyncMock(return_value="Check your email")
        adapter.send_response = AsyncMock()
        transport.adapter = adapter
        transport.on_event = AsyncMock()
        asyncio.run(transport._process_update({"Body": "/login carol@example.com", "From": WA},
                                              world.get_whatsapp_binding(AGENT)))
        return adapter, transport

    def test_a_blocked_senders_whatsapp_command_sends_nothing(self, world):
        _block(("whatsapp", WA))
        adapter, transport = self._whatsapp(world)
        adapter.handle_command.assert_not_awaited()
        adapter.send_response.assert_not_awaited()
        transport.on_event.assert_not_awaited()

    def test_an_unblocked_senders_whatsapp_command_is_answered(self, world):
        adapter, _ = self._whatsapp(world)
        adapter.handle_command.assert_awaited_once()


class TestABlockIsNeverPermanentByAccident:
    def test_a_blocked_client_whose_chat_links_are_gone_stays_on_the_roster(self, world):
        """A bot unbind deletes the chat links; the block survives, and the
        client's messages drop before re-recording, so they never came back to
        the roster and unblock answered 404."""
        _block(("telegram", "1002"))
        world.delete_telegram_binding(AGENT)
        row = _roster()[("telegram", "1002")]
        assert row["access"] == "blocked"
        from services import client_roster_service as svc
        svc.unblock_client(AGENT, "telegram", "1002")
        assert ("telegram", "1002") not in _roster()


class TestAClientHeldAtTheGateIsReachable:
    def test_a_pending_requester_with_no_chat_link_is_listed_and_blockable(self, world):
        """Chat links are written only after the access gate and only for DMs
        (#1533), so a stranger held there, or a group-only sender, had no row.
        Their access request names their channel id (ent#737)."""
        world.upsert_access_request(AGENT, "dave@example.com", "telegram",
                                    identity={"requester_channel_id": "1003", "requester_name": "Dave",
                                               "requester_handle": "@dave"})
        row = _roster()[("telegram", "1003")]
        assert row["access"] == "pending" and row["access_request_id"]
        assert row["display_name"] == "Dave"
        _block(("telegram", "1003"))
        assert _roster()[("telegram", "1003")]["access"] == "blocked"
        assert world.is_channel_client_blocked(AGENT, "telegram", "1003")


class TestProactiveMessages:
    @pytest.mark.parametrize("channel, client", [("telegram", "1001"), ("whatsapp", WA)])
    def test_the_agent_cannot_message_a_blocked_client_on_that_channel(self, world, channel, client):
        from services import proactive_message_service as pms
        _block((channel, client))
        email = ALICE if channel == "telegram" else CAROL
        deliver = pms.ProactiveMessageService()._deliver_telegram if channel == "telegram" \
            else pms.ProactiveMessageService()._deliver_whatsapp
        with pytest.raises(pms.ChannelDeliveryError, match="recipient_blocked"):
            asyncio.run(deliver(AGENT, email, "hi"))
