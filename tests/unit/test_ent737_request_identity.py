"""trinity-enterprise#737 — a channel access request names who asked.

Before: a request stored ``(agent, email, channel, requested_at, status)``, so
the owner approved a bare email. Now it also records who asked, as the channel
saw them (``db.access_requests.REQUESTER_FIELDS``), on every path that creates
one. Pinned here:

- **Storage** (real DB, both backends): the identity is stored, a refresh from a
  channel that exposes less replaces it (never inherits), and a decision keeps it.
- **Capture**: what each adapter reads from a real parsed message — Telegram's
  id, @username and first/last name; WhatsApp's number and profile name; Slack's
  member id, handle, display name, channel and thread. Fields a channel does not
  expose are absent.
- **The per-message gate** passes the adapter's identity to the request, and a
  failure to read it still records the request (thin, not lost). The two
  ``/login`` flows are pinned in their own adapter tests.
- **The owner's view**: ``identity_captured`` (False for the public link and
  for older rows), and ``known_as``, the same channel identity seen before,
  limited to agents the viewer manages.
"""
import asyncio
import os
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent.parent / "src" / "backend"
for p in (str(_BACKEND), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)
from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

ALICE, BOB, ADMIN = "alice", "bob", "root"
TG_ID = "424242"
IDENTITY = {"requester_channel_id": TG_ID, "requester_handle": "carol_tg",
            "requester_name": "Carol Jones"}


@pytest.fixture
def world(db_backend, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    from database import db
    from db_models import UserCreate
    for name, role in ((ALICE, "creator"), (BOB, "creator"), (ADMIN, "admin")):
        db.create_user(UserCreate(username=name, password="x", role=role, email=f"{name}@example.com"))
    for agent, owner in (("a-one", ALICE), ("a-two", ALICE), ("b-one", BOB)):
        db.register_agent_owner(agent, owner)
    return db


# --------------------------------------------------------------------- storage

class TestStorage:
    def test_the_identity_is_stored_with_the_request(self, world):
        row = world.upsert_access_request("a-one", "Carol@Example.com", "telegram", identity=IDENTITY)
        assert row["email"] == "carol@example.com"
        assert {k: row[k] for k in IDENTITY} == IDENTITY
        assert row["requester_chat_id"] is None and row["requester_thread_id"] is None

    def test_a_refresh_from_a_channel_that_exposes_less_never_inherits(self, world):
        world.upsert_access_request("a-one", "carol@example.com", "telegram", identity=IDENTITY)
        row = world.upsert_access_request("a-one", "carol@example.com", "web")
        assert row["channel"] == "web"
        assert row["requester_channel_id"] is None and row["requester_name"] is None

    def test_a_decision_keeps_the_identity(self, world):
        req = world.upsert_access_request("a-one", "carol@example.com", "telegram", identity=IDENTITY)
        world.decide_access_request(req["id"], True, 1)
        decided = world.get_access_request(req["id"])
        assert decided["status"] == "approved"
        assert decided["requester_channel_id"] == TG_ID and decided["requester_name"] == "Carol Jones"

    def test_blank_values_are_absent_and_long_ones_bounded(self, world):
        row = world.upsert_access_request("a-one", "c@example.com", "telegram",
                                          identity={"requester_channel_id": "  ", "requester_name": "x" * 900})
        assert row["requester_channel_id"] is None
        assert len(row["requester_name"]) == 256


# --------------------------------------------------------------------- capture

class TestCapture:
    def test_telegram_reads_id_handle_and_name_from_the_update(self):
        from adapters.telegram_adapter import TelegramAdapter
        a = TelegramAdapter()
        msg = a.parse_message({"_bot_id": "1", "_bot_username": "b", "_agent_name": "a-one", "message": {
            "message_id": 9, "text": "hi", "chat": {"id": 4242, "type": "private"},
            "from": {"id": 4242, "username": "carol_tg", "first_name": "Carol", "last_name": "Jones"}}})
        assert a.requester_identity(msg) == {
            "requester_channel_id": "4242", "requester_handle": "carol_tg", "requester_name": "Carol Jones"}

    def test_telegram_without_a_username_has_no_handle(self):
        from adapters.telegram_adapter import TelegramAdapter
        a = TelegramAdapter()
        msg = a.parse_message({"_bot_id": "1", "_agent_name": "a-one", "message": {
            "message_id": 9, "text": "hi", "chat": {"id": 7, "type": "private"},
            "from": {"id": 7, "first_name": "Dee"}}})
        assert a.requester_identity(msg) == {"requester_channel_id": "7", "requester_name": "Dee"}

    def test_whatsapp_reads_the_number_and_profile_name(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter
        a = WhatsAppAdapter()
        msg = a.parse_message({"From": "whatsapp:+15550001111", "Body": "hi", "MessageSid": "SM1",
                               "ProfileName": "Carol", "_agent_name": "a-one", "_binding_id": 1})
        assert a.requester_identity(msg) == {
            "requester_channel_id": "whatsapp:+15550001111", "requester_name": "Carol"}

    def test_slack_reads_the_enriched_identity_channel_and_thread(self):
        from adapters.base import NormalizedMessage
        from adapters.slack_adapter import SlackAdapter
        msg = NormalizedMessage(sender_id="U123", text="hi", channel_id="C9", thread_id="171.5",
                                timestamp="0", metadata={"sender_display_name": "Carol Jones",
                                                         "sender_username": "carol"})
        assert SlackAdapter().requester_identity(msg) == {
            "requester_channel_id": "U123", "requester_handle": "carol", "requester_name": "Carol Jones",
            "requester_chat_id": "C9", "requester_thread_id": "171.5"}


# ------------------------------------------------------------ the per-message gate

def _gate(identity_side_effect=None):
    from test_message_router_access_gate import _env, _make_adapter, _make_message, _run
    from adapters.message_router import ChannelMessageRouter
    adapter = _make_adapter()
    adapter.resolve_verified_email = AsyncMock(return_value="carol@example.com")
    adapter.requester_identity = MagicMock(return_value=IDENTITY, side_effect=identity_side_effect)
    with _env({"require_email": True, "open_access": False, "group_auth_mode": "none"}) as (db, service):
        _run(ChannelMessageRouter(), adapter, _make_message())
    return db, service, adapter


def test_the_gate_records_who_asked():
    db, service, adapter = _gate()
    db.upsert_access_request.assert_called_once_with("agent1", "carol@example.com", "telegram",
                                                     identity=IDENTITY)
    service.execute_task.assert_not_awaited()


def test_a_failure_to_read_the_identity_still_records_the_request():
    db, service, _ = _gate(identity_side_effect=RuntimeError("boom"))
    db.upsert_access_request.assert_called_once_with("agent1", "carol@example.com", "telegram",
                                                     identity=None)
    service.execute_task.assert_not_awaited()


# ------------------------------------------------------------- the owner's view

def _list(viewer, agent="a-one", status="pending"):
    from services import access_request_service
    rows = access_request_service.list_access_requests(agent, status, viewer)
    return {r["email"]: r for r in rows}


class TestOwnersView:
    def test_a_request_without_identity_says_so(self, world):
        world.upsert_access_request("a-one", "web@example.com", "web")
        row = _list(ALICE)["web@example.com"]
        assert row["identity_captured"] is False and row["known_as"] == []

    def test_a_known_person_is_recognised_on_agents_the_viewer_manages_only(self, world):
        # Same Telegram account: approved on alice's a-two, verified on alice's
        # a-two's bot, approved on bob's b-one. Now asking on a-one.
        prior = world.upsert_access_request("a-two", "carol@work.example", "telegram", identity=IDENTITY)
        world.decide_access_request(prior["id"], True, 1)
        theirs = world.upsert_access_request("b-one", "carol@example.com", "telegram", identity=IDENTITY)
        world.decide_access_request(theirs["id"], True, 1)
        world.create_telegram_binding(agent_name="a-two", bot_token="t:2", bot_id="2")
        tg = world.get_telegram_binding("a-two")["id"]
        world.record_telegram_inbound(tg, TG_ID, "carol_tg")
        world.set_telegram_verified_email(tg, TG_ID, "carol@work.example")
        world.upsert_access_request("a-one", "carol@example.com", "telegram", identity=IDENTITY)

        mine = _list(ALICE)["carol@example.com"]
        assert mine["identity_captured"] is True
        assert sorted((k["agent_name"], k["status"], k["email"]) for k in mine["known_as"]) == [
            ("a-two", "approved", "carol@work.example"), ("a-two", "verified", "carol@work.example")]
        # An admin manages every agent, so sees bob's too.
        assert ("b-one", "approved", "carol@example.com") in {
            (k["agent_name"], k["status"], k["email"]) for k in _list(ADMIN)["carol@example.com"]["known_as"]}

    def test_the_request_itself_is_not_its_own_history(self, world):
        world.create_telegram_binding(agent_name="a-one", bot_token="t:1", bot_id="1")
        tg = world.get_telegram_binding("a-one")["id"]
        world.record_telegram_inbound(tg, TG_ID, "carol_tg")
        world.set_telegram_verified_email(tg, TG_ID, "carol@example.com")
        world.upsert_access_request("a-one", "carol@example.com", "telegram", identity=IDENTITY)
        assert _list(ALICE)["carol@example.com"]["known_as"] == []

    def test_the_list_route_returns_the_person(self, world):
        from routers.sharing import list_access_requests_endpoint
        world.upsert_access_request("a-one", "carol@example.com", "telegram", identity=IDENTITY)
        out = asyncio.run(list_access_requests_endpoint("a-one", SimpleNamespace(username=ALICE), "pending"))
        assert len(out) == 1
        got = out[0].model_dump()
        assert {k: got[k] for k in IDENTITY} == IDENTITY and got["identity_captured"] is True
