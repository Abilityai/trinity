"""Unit tests for the Sharing-tab client message counter (#1533).

Bug: the roster showed ``message_count = 0`` for every client, ``last_active``
was frozen at ``/login`` time, and clients who never ran ``/login`` never
appeared at all. Root cause: the chat-link write path
(``get_or_create_chat_link`` + ``increment_message_count``) was dead — zero
callers outside the ``database.py`` facade.

Fix: a ``ChannelAdapter.record_inbound_activity`` hook, called once per
*delivered* DM by ``ChannelMessageRouter._handle_message_inner`` after the
access gate, backed by one atomic upsert (``record_inbound``) per channel.

These tests deliberately run the DB assertions against the REAL dual-backend
``db_backend`` harness and assert the **roster read-back**, not "the mock was
called". A wholesale ``sys.modules["database"].db = MagicMock()`` stub passes
green even when the facade delegation is missing or renamed — see
``docs/memory/learnings.md`` (2026-07-06). ``TestFacadeDelegation`` pins the
delegation itself, and also guards against the dead methods being reintroduced.

Module: src/backend/db/telegram_channels.py
        src/backend/db/whatsapp_channels.py
        src/backend/adapters/base.py
        src/backend/adapters/message_router.py
Issue:  Abilityai/trinity#1533
"""

import asyncio
import os
import secrets
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# IMPORTANT: set REDIS_URL BEFORE any backend import (Issue #589 hard-fail).
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import db_backend  # noqa: E402,F401

from adapters.base import ChannelAdapter, NormalizedMessage  # noqa: E402
from adapters.message_router import ChannelMessageRouter  # noqa: E402

_MR = sys.modules[ChannelMessageRouter.__module__]

TG_AGENT = "agent-tg"
WA_AGENT = "agent-wa"
WA_PHONE = "whatsapp:+15559998888"


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    """create_binding encrypts the bot/auth token; needs a key."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    yield


def _telegram_ops_with_binding():
    from db import telegram_channels as tg_db
    ops = tg_db.TelegramChannelOperations()
    ops.create_binding(agent_name=TG_AGENT, bot_token="tok", bot_id="111")
    return ops, ops.get_binding_by_agent(TG_AGENT)["id"]


def _whatsapp_ops_with_binding():
    from db import whatsapp_channels as wa_db
    ops = wa_db.WhatsAppChannelOperations()
    ops.create_binding(
        agent_name=WA_AGENT,
        account_sid="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        auth_token="tok",
        from_number="whatsapp:+15551230000",
    )
    return ops, ops.get_binding_by_agent(WA_AGENT)["id"]


def _only_client(ops, agent_name):
    clients = ops.list_clients_for_agent(agent_name)
    assert len(clients) == 1, clients
    return clients[0]


# ---------------------------------------------------------------------------
# Telegram — counter, last_active, roster membership
# ---------------------------------------------------------------------------


class TestTelegramCounter:
    def test_first_inbound_creates_row_and_counts_one(self, db_backend):
        ops, bind = _telegram_ops_with_binding()
        ops.record_inbound(bind, "1001", "alice")

        c = _only_client(ops, TG_AGENT)
        assert c["message_count"] == 1
        assert c["identity"] == "@alice"
        assert c["last_active"] is not None

    def test_repeated_inbound_increments_in_place(self, db_backend):
        ops, bind = _telegram_ops_with_binding()
        ops.record_inbound(bind, "1001", "alice")
        ops.record_inbound(bind, "1001", "alice")
        ops.record_inbound(bind, "1001", "alice")

        # Upsert must increment, not insert duplicates.
        c = _only_client(ops, TG_AGENT)
        assert c["message_count"] == 3

    def test_last_active_advances_on_inbound(self, db_backend):
        """The regression the issue body got wrong: last_active was frozen at /login."""
        ops, bind = _telegram_ops_with_binding()
        ops.set_verified_email(bind, "1001", "alice@example.com")
        frozen = _only_client(ops, TG_AGENT)["last_active"]

        ops.record_inbound(bind, "1001", "alice")
        moved = _only_client(ops, TG_AGENT)["last_active"]

        # Same ISO-Z format, microsecond precision → lexicographic compare is safe.
        assert moved > frozen

    def test_client_who_never_logged_in_appears_in_roster(self, db_backend):
        """Previously invisible: rows were only ever created by /login."""
        ops, bind = _telegram_ops_with_binding()
        ops.record_inbound(bind, "1001", "alice")

        c = _only_client(ops, TG_AGENT)
        assert c["verified_email"] is None
        assert c["message_count"] == 1

    def test_verified_email_binds_onto_a_hook_created_row(self, db_backend):
        """Regression guard: set_verified_email must still work on a pre-created row."""
        ops, bind = _telegram_ops_with_binding()
        ops.record_inbound(bind, "1001", "alice")

        assert ops.set_verified_email(bind, "1001", "alice@example.com") is True

        c = _only_client(ops, TG_AGENT)
        assert c["verified_email"] == "alice@example.com"
        assert c["message_count"] == 1  # preserved, not reset

    def test_username_backfilled_when_login_created_the_row_first(self, db_backend):
        """/login inserts without a username; the first inbound message supplies it."""
        ops, bind = _telegram_ops_with_binding()
        ops.set_verified_email(bind, "1001", "alice@example.com")
        assert _only_client(ops, TG_AGENT)["display_name"] is None

        ops.record_inbound(bind, "1001", "alice")

        c = _only_client(ops, TG_AGENT)
        assert c["display_name"] == "alice"
        assert c["identity"] == "@alice"

    def test_known_username_is_not_clobbered_by_a_later_null(self, db_backend):
        """COALESCE(excluded, existing): a message without a username keeps the old one."""
        ops, bind = _telegram_ops_with_binding()
        ops.record_inbound(bind, "1001", "alice")
        ops.record_inbound(bind, "1001", None)

        c = _only_client(ops, TG_AGENT)
        assert c["display_name"] == "alice"
        assert c["message_count"] == 2

    def test_counts_are_isolated_per_client(self, db_backend):
        ops, bind = _telegram_ops_with_binding()
        ops.record_inbound(bind, "1001", "alice")
        ops.record_inbound(bind, "1001", "alice")
        ops.record_inbound(bind, "1002", "bob")

        by_identity = {c["identity"]: c for c in ops.list_clients_for_agent(TG_AGENT)}
        assert by_identity["@alice"]["message_count"] == 2
        assert by_identity["@bob"]["message_count"] == 1


# ---------------------------------------------------------------------------
# WhatsApp — same contract
# ---------------------------------------------------------------------------


class TestWhatsAppCounter:
    def test_first_inbound_creates_row_and_counts_one(self, db_backend):
        ops, bind = _whatsapp_ops_with_binding()
        ops.record_inbound(bind, WA_PHONE, "Carol")

        c = _only_client(ops, WA_AGENT)
        assert c["message_count"] == 1
        assert c["identity"] == WA_PHONE
        assert c["display_name"] == "Carol"

    def test_repeated_inbound_increments_in_place(self, db_backend):
        ops, bind = _whatsapp_ops_with_binding()
        ops.record_inbound(bind, WA_PHONE, "Carol")
        ops.record_inbound(bind, WA_PHONE, "Carol")

        assert _only_client(ops, WA_AGENT)["message_count"] == 2

    def test_last_active_advances_on_inbound(self, db_backend):
        ops, bind = _whatsapp_ops_with_binding()
        ops.set_verified_email(bind, WA_PHONE, "carol@example.com")
        frozen = _only_client(ops, WA_AGENT)["last_active"]

        ops.record_inbound(bind, WA_PHONE, "Carol")

        assert _only_client(ops, WA_AGENT)["last_active"] > frozen

    def test_verified_email_binds_onto_a_hook_created_row(self, db_backend):
        ops, bind = _whatsapp_ops_with_binding()
        ops.record_inbound(bind, WA_PHONE, "Carol")
        ops.set_verified_email(bind, WA_PHONE, "carol@example.com")

        c = _only_client(ops, WA_AGENT)
        assert c["verified_email"] == "carol@example.com"
        assert c["message_count"] == 1

    def test_profile_name_backfilled_when_login_created_the_row_first(self, db_backend):
        ops, bind = _whatsapp_ops_with_binding()
        ops.set_verified_email(bind, WA_PHONE, "carol@example.com")
        assert _only_client(ops, WA_AGENT)["display_name"] is None

        ops.record_inbound(bind, WA_PHONE, "Carol")

        assert _only_client(ops, WA_AGENT)["display_name"] == "Carol"


# ---------------------------------------------------------------------------
# Facade delegation (learnings.md 2026-07-06)
# ---------------------------------------------------------------------------


class TestFacadeDelegation:
    """A wholesale-mocked `database` can't see these — assert them structurally."""

    def test_record_telegram_inbound_delegates_to_ops(self):
        import database as database_module

        captured = {}
        fake_self = SimpleNamespace(
            _telegram_channel_ops=SimpleNamespace(
                record_inbound=lambda *a: captured.setdefault("args", a)
            )
        )
        database_module.DatabaseManager.record_telegram_inbound(fake_self, 7, "1001", "alice")
        assert captured["args"] == (7, "1001", "alice")

    def test_record_whatsapp_inbound_delegates_to_ops(self):
        import database as database_module

        captured = {}
        fake_self = SimpleNamespace(
            _whatsapp_channel_ops=SimpleNamespace(
                record_inbound=lambda *a: captured.setdefault("args", a)
            )
        )
        database_module.DatabaseManager.record_whatsapp_inbound(fake_self, 9, WA_PHONE, "Carol")
        assert captured["args"] == (9, WA_PHONE, "Carol")

    @pytest.mark.parametrize(
        "dead_method",
        [
            "get_or_create_telegram_chat_link",
            "increment_telegram_message_count",
            "get_or_create_whatsapp_chat_link",
            "increment_whatsapp_message_count",
        ],
    )
    def test_dead_chat_link_methods_are_gone(self, dead_method):
        """Guard against reintroducing the dead write path that caused #1533."""
        import database as database_module

        assert not hasattr(database_module.DatabaseManager, dead_method)


# ---------------------------------------------------------------------------
# ABC default + router wiring
# ---------------------------------------------------------------------------


def test_abc_default_record_inbound_activity_is_a_noop():
    """Slack/VoIP inherit this — it must never raise (Invariant #9)."""

    class _Bare(ChannelAdapter):
        channel_type = "bare"

        def get_rate_key(self, message): return "rk"
        def get_session_identifier(self, message): return "sid"
        def get_source_identifier(self, message): return "src"
        def get_bot_token(self, message): return "tok"
        def parse_message(self, raw_event): return None
        async def send_response(self, channel_id, response, thread_id=None): return None
        async def get_agent_name(self, message): return "agent1"

    msg = NormalizedMessage(sender_id="u1", text="hi", channel_id="c1", timestamp="t")
    assert asyncio.run(_Bare().record_inbound_activity(msg, "agent1")) is None


class TestAdapterOverrides:
    """Execute the REAL adapter bodies.

    The router test injects a MagicMock adapter, so without these the override
    bodies never run and a wrong metadata key (`username` vs `sender_username`)
    or a mistyped facade name would ship green. This is the wholesale-mock
    blindness of learnings.md 2026-07-06, one layer up from the facade.
    """

    def test_telegram_override_passes_binding_id_sender_and_username(self):
        from adapters.telegram_adapter import TelegramAdapter

        mock_db = MagicMock()
        mock_db.get_telegram_binding.return_value = {"id": 42}
        # `username` is the key TelegramAdapter.parse_message actually writes.
        msg = NormalizedMessage(
            sender_id="1001", text="hi", channel_id="1001", timestamp="t",
            metadata={"username": "alice", "is_group": False},
        )
        with patch("adapters.telegram_adapter.db", mock_db):
            asyncio.run(TelegramAdapter().record_inbound_activity(msg, "agent1"))

        mock_db.get_telegram_binding.assert_called_once_with("agent1")
        mock_db.record_telegram_inbound.assert_called_once_with(42, "1001", "alice")

    def test_telegram_override_is_a_noop_without_a_binding(self):
        from adapters.telegram_adapter import TelegramAdapter

        mock_db = MagicMock()
        mock_db.get_telegram_binding.return_value = None
        msg = NormalizedMessage(sender_id="1001", text="hi", channel_id="1001", timestamp="t")
        with patch("adapters.telegram_adapter.db", mock_db):
            asyncio.run(TelegramAdapter().record_inbound_activity(msg, "agent1"))

        mock_db.record_telegram_inbound.assert_not_called()

    def test_whatsapp_override_passes_binding_id_phone_and_profile_name(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter

        mock_db = MagicMock()
        mock_db.get_whatsapp_binding.return_value = {"id": 7}
        # `wa_user_name` is the key WhatsAppAdapter.parse_message actually writes.
        msg = NormalizedMessage(
            sender_id=WA_PHONE, text="hi", channel_id=WA_PHONE, timestamp="t",
            metadata={"wa_user_name": "Carol", "is_group": False},
        )
        with patch("adapters.whatsapp_adapter.db", mock_db):
            asyncio.run(WhatsAppAdapter().record_inbound_activity(msg, "agent1"))

        mock_db.get_whatsapp_binding.assert_called_once_with("agent1")
        mock_db.record_whatsapp_inbound.assert_called_once_with(7, WA_PHONE, "Carol")

    def test_whatsapp_override_is_a_noop_without_a_binding(self):
        from adapters.whatsapp_adapter import WhatsAppAdapter

        mock_db = MagicMock()
        mock_db.get_whatsapp_binding.return_value = None
        msg = NormalizedMessage(sender_id=WA_PHONE, text="hi", channel_id=WA_PHONE, timestamp="t")
        with patch("adapters.whatsapp_adapter.db", mock_db):
            asyncio.run(WhatsAppAdapter().record_inbound_activity(msg, "agent1"))

        mock_db.record_whatsapp_inbound.assert_not_called()

    def test_metadata_keys_match_what_parse_message_writes(self):
        """Pin the two metadata keys the overrides read against the parsers that write them."""
        import inspect
        from adapters import telegram_adapter, whatsapp_adapter

        tg_src = inspect.getsource(telegram_adapter.TelegramAdapter.parse_message)
        wa_src = inspect.getsource(whatsapp_adapter.WhatsAppAdapter.parse_message)
        assert '"username": username' in tg_src
        assert '"wa_user_name": wa_user_name' in wa_src


def _make_adapter(channel: str = "telegram") -> MagicMock:
    a = MagicMock()
    a.channel_type = channel
    a.get_agent_name = AsyncMock(return_value="agent1")
    a.enrich_message = AsyncMock(return_value=None)
    a.handle_verification = AsyncMock(return_value=True)
    a.resolve_verified_email = AsyncMock(return_value=None)
    a.record_inbound_activity = AsyncMock(return_value=None)
    a.is_group_verified = AsyncMock(return_value=True)
    a.set_group_verified = AsyncMock()
    a.prompt_group_auth = AsyncMock()
    a.prompt_auth = AsyncMock()
    a.indicate_processing = AsyncMock()
    a.indicate_done = AsyncMock()
    a.send_response = AsyncMock()
    a.on_response_sent = AsyncMock()
    a.get_bot_token = MagicMock(return_value="tok")
    a.get_rate_key = MagicMock(return_value="rk")
    a.get_session_identifier = MagicMock(return_value="sid")
    a.get_source_identifier = MagicMock(return_value="src@example.com")
    return a


def _make_message(is_group: bool = False) -> NormalizedMessage:
    return NormalizedMessage(
        sender_id="u1",
        text="hello",
        channel_id="c1",
        timestamp="2026-01-01T00:00:00Z",
        metadata={"is_group": is_group},
    )


_OPEN = {"require_email": False, "open_access": True, "group_auth_mode": "none"}


@contextmanager
def _env(policy: dict):
    db = MagicMock()
    db.get_access_policy.return_value = policy
    db.email_has_agent_access.return_value = False
    db.get_or_create_public_chat_session.return_value = {"id": "s1"}
    db.build_public_chat_context.return_value = "ctx-prompt"
    db.get_or_create_public_user_memory.return_value = {}
    db.increment_public_user_memory_count.return_value = 0

    container = MagicMock()
    container.status = "running"

    result = MagicMock()
    result.status = "success"
    result.response = "agent reply"
    result.error = None
    result.cost = 0.0
    result.execution_id = "e1"
    service = MagicMock()
    service.execute_task = AsyncMock(return_value=result)

    with patch.object(_MR, "db", db), \
         patch.object(_MR, "get_agent_container", return_value=container), \
         patch.object(_MR, "get_task_execution_service", return_value=service), \
         patch.object(_MR, "_check_rate_limit", return_value=True), \
         patch.object(_MR, "process_voice", new=AsyncMock(return_value="")), \
         patch.object(_MR, "format_user_memory_block", return_value=None), \
         patch.object(_MR, "summarize_user_memory_background", new=AsyncMock()):
        yield db, service


def _run(router, adapter, message):
    asyncio.run(router._handle_message_inner(adapter, message))


class TestRouterWiring:
    def test_delivered_dm_records_inbound_activity(self):
        router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message()
        with _env(_OPEN) as (_db, service):
            _run(router, adapter, message)
        adapter.record_inbound_activity.assert_awaited_once_with(message, "agent1")
        service.execute_task.assert_awaited_once()

    def test_group_message_is_not_counted(self):
        """Chat links are DM-keyed; counting groups would list non-DM members."""
        router, adapter = ChannelMessageRouter(), _make_adapter()
        message = _make_message(is_group=True)
        with _env(_OPEN) as (_db, service):
            _run(router, adapter, message)
        adapter.record_inbound_activity.assert_not_awaited()
        service.execute_task.assert_awaited_once()  # the group turn still runs

    def test_access_denied_message_is_not_counted(self):
        """Hook sits after the gate: a stranger cannot create unbounded rows."""
        router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message()
        adapter.resolve_verified_email = AsyncMock(return_value="alice@example.com")
        restrictive = {"require_email": False, "open_access": False, "group_auth_mode": "none"}
        with _env(restrictive) as (db, service):
            db.email_has_agent_access.return_value = False
            _run(router, adapter, message)
        adapter.record_inbound_activity.assert_not_awaited()
        service.execute_task.assert_not_awaited()

    def test_counter_failure_never_blocks_message_processing(self):
        """Best-effort: a DB error on the counter must not cost the user their reply."""
        router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message()
        adapter.record_inbound_activity = AsyncMock(side_effect=RuntimeError("db down"))
        with _env(_OPEN) as (_db, service):
            _run(router, adapter, message)
        service.execute_task.assert_awaited_once()
        adapter.send_response.assert_awaited()
