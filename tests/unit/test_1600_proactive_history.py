"""#1600 — proactive messages are persisted to channel session history.

The bug: `send_message` (#321) delivered a proactive message and wrote nothing to
`public_chat_messages`. The next inbound turn builds context from
`db.build_public_chat_context(session_id, ...)`, which only ever contained turns
the message router persisted — so the agent had no record of its own outreach. It
would repeat itself, contradict the message, or fail to parse the reply ("yes,
sounds good" — to what?).

**The load-bearing property is identifier equality.** Persisting into *a* session
is worthless; it has to be the SAME session the recipient's next inbound message
resolves to. Otherwise the write succeeds, the bug survives, and nothing looks
broken. `test_*_identifier_matches_inbound_router` is that assertion, per channel.

Note the issue's suggested fix — key off `telegram_chat_links.session_id` /
`whatsapp_chat_links.session_id` — cannot work: those columns are **never written
by any code path** (verified across the repo; they are read in three SELECTs and
nowhere else). Keying on them would persist nothing, silently. The fix instead
derives the key through each adapter's own `get_session_identifier()`, so it
can't drift from the router.

Issue: https://github.com/abilityai/trinity/issues/1600
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_backend = str(Path(__file__).resolve().parents[2] / "src" / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Identifier parity — the property the whole fix rests on
# --------------------------------------------------------------------------- #
class TestSessionIdentifierParity:
    """The proactive key must equal the key an inbound DM resolves to.

    Built by driving each adapter's real `get_session_identifier` with (a) a
    synthetic inbound DM, and (b) the probe shape the proactive service builds.
    If someone changes an adapter's format, these fail — which is the point:
    duplicating the format in the service is exactly the drift this guards.
    """

    def test_telegram_identifier_matches_inbound_router(self):
        from adapters.base import NormalizedMessage
        from adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter()
        bot_id, tg_user = "bot-77", "123456"

        # What the router sees for a real inbound DM (chat_id == user_id).
        inbound = NormalizedMessage(
            sender_id=tg_user, text="hi", channel_id=tg_user, timestamp="t",
            metadata={"bot_id": bot_id, "is_group": False},
        )
        # What the proactive service builds after a successful send.
        probe = NormalizedMessage(
            sender_id=tg_user, text="", channel_id=tg_user, timestamp="",
            metadata={"bot_id": bot_id, "is_group": False},
        )
        assert adapter.get_session_identifier(probe) == adapter.get_session_identifier(inbound)

    def test_whatsapp_identifier_matches_inbound_router(self):
        from adapters.base import NormalizedMessage
        from adapters.whatsapp_adapter import WhatsAppAdapter

        adapter = WhatsAppAdapter()
        phone, binding_id = "whatsapp:+14155551234", 7

        inbound = NormalizedMessage(
            sender_id=phone, text="hi", channel_id=phone, timestamp="t",
            metadata={"binding_id": binding_id},
        )
        probe = NormalizedMessage(
            sender_id=phone, text="", channel_id=phone, timestamp="",
            metadata={"binding_id": binding_id},
        )
        assert adapter.get_session_identifier(probe) == adapter.get_session_identifier(inbound)

    def test_slack_identifier_matches_inbound_dm(self):
        from adapters.base import NormalizedMessage
        from adapters.slack_adapter import SlackAdapter

        adapter = SlackAdapter()
        team, user, dm = "T1", "U9", "D5"

        inbound = NormalizedMessage(
            sender_id=user, text="hi", channel_id=dm, timestamp="t",
            metadata={"team_id": team, "is_dm": True},
        )
        probe = NormalizedMessage(
            sender_id=user, text="", channel_id=dm, timestamp="",
            metadata={"team_id": team, "is_dm": True},
        )
        assert adapter.get_session_identifier(probe) == adapter.get_session_identifier(inbound)

    def test_slack_probe_takes_the_dm_branch_not_the_thread_branch(self):
        """Without `is_dm`, Slack keys on `team:channel:thread` — a different
        session, and thread would be None. The probe must set is_dm."""
        from adapters.base import NormalizedMessage
        from adapters.slack_adapter import SlackAdapter

        adapter = SlackAdapter()
        dm_key = adapter.get_session_identifier(NormalizedMessage(
            sender_id="U9", text="", channel_id="D5", timestamp="",
            metadata={"team_id": "T1", "is_dm": True},
        ))
        thread_key = adapter.get_session_identifier(NormalizedMessage(
            sender_id="U9", text="", channel_id="D5", timestamp="",
            metadata={"team_id": "T1"},
        ))
        assert dm_key != thread_key
        assert dm_key == "T1:U9:D5"


# --------------------------------------------------------------------------- #
# The identifier the SERVICE actually produces
# --------------------------------------------------------------------------- #
class TestDeliveryPathProducesTheInboundKey:
    """Drive the real `_deliver_*` and assert the key it returns.

    `TestSessionIdentifierParity` above compares two probes *written in this
    file* — it proves the adapters are self-consistent, which was never in
    doubt, and would pass even if the service built its probe wrong. These tests
    close that hole: they exercise the service's own probe construction and
    compare the result against the key an inbound DM resolves to.

    Caught in review by sabotaging the service (`bot_id` -> `botid_TYPO`, which
    silently yields `unknown:...` and a DIFFERENT session): every other test in
    this file still passed. These fail.
    """

    @pytest.mark.asyncio
    async def test_telegram_delivery_returns_the_inbound_session_key(self, svc, monkeypatch):
        from adapters.base import NormalizedMessage
        from adapters import telegram_adapter as tg_mod

        svc.db.get_telegram_binding.return_value = {"id": 1, "bot_id": "bot-77"}
        svc.db.get_telegram_chat_link_by_verified_email.return_value = {
            "telegram_user_id": "123456", "session_id": None,
        }
        svc.db.get_telegram_bot_token.return_value = "xoxb-fake"

        async def _send(**kw):
            return {"message_id": 42}
        monkeypatch.setattr(tg_mod.TelegramAdapter, "_send_message", staticmethod(_send))

        result = await svc.service._deliver_telegram("agent-a", "user@example.com", "ping")
        assert result.success

        expected = tg_mod.TelegramAdapter().get_session_identifier(NormalizedMessage(
            sender_id="123456", text="hi", channel_id="123456", timestamp="t",
            metadata={"bot_id": "bot-77", "is_group": False},
        ))
        assert result.session_identifier == expected, (
            f"service derived {result.session_identifier!r}, inbound resolves to "
            f"{expected!r} — the proactive turn would land in a different session"
        )
        assert "bot-77" in result.session_identifier, "bot_id never reached the key"

    @pytest.mark.asyncio
    async def test_slack_delivery_returns_the_inbound_dm_key(self, svc, monkeypatch):
        from adapters.base import NormalizedMessage
        from adapters.slack_adapter import SlackAdapter
        import services.slack_service as slack_mod

        svc.db.get_all_slack_workspaces.return_value = [
            {"team_id": "T1", "bot_token": "xoxb-fake"}
        ]

        async def _by_email(token, email):
            return {"id": "U9"}

        async def _open_dm(token, uid):
            return "D5"

        async def _send(**kw):
            return True, None

        monkeypatch.setattr(slack_mod.slack_service, "get_user_by_email", _by_email)
        monkeypatch.setattr(slack_mod.slack_service, "open_dm_channel", _open_dm)
        monkeypatch.setattr(slack_mod.slack_service, "send_message", _send)

        result = await svc.service._deliver_slack("agent-a", "user@example.com", "ping")
        assert result.success

        expected = SlackAdapter().get_session_identifier(NormalizedMessage(
            sender_id="U9", text="hi", channel_id="D5", timestamp="t",
            metadata={"team_id": "T1", "is_dm": True},
        ))
        assert result.session_identifier == expected
        assert result.session_identifier == "T1:U9:D5"

    @pytest.mark.asyncio
    async def test_failed_send_carries_no_session_key(self, svc, monkeypatch):
        """A failed delivery must not hand the caller a key to persist against."""
        from adapters import telegram_adapter as tg_mod

        svc.db.get_telegram_binding.return_value = {"id": 1, "bot_id": "bot-77"}
        svc.db.get_telegram_chat_link_by_verified_email.return_value = {
            "telegram_user_id": "123456",
        }
        svc.db.get_telegram_bot_token.return_value = "xoxb-fake"

        async def _send(**kw):
            return None  # Telegram returned nothing => send failed
        monkeypatch.setattr(tg_mod.TelegramAdapter, "_send_message", staticmethod(_send))

        result = await svc.service._deliver_telegram("agent-a", "user@example.com", "ping")
        assert result.success is False
        assert result.session_identifier is None


# --------------------------------------------------------------------------- #
# Persistence behaviour
# --------------------------------------------------------------------------- #
@pytest.fixture
def svc(monkeypatch):
    """ProactiveMessageService with its module-level `db` + audit names stubbed.

    Patches the names **as bound in the module** rather than stubbing
    `sys.modules` and forcing a re-import. The re-import approach poisons
    `tests/unit/test_1609_proactive_rate_limits.py` when this file runs first:
    re-importing rebinds the `services.proactive_message_service` attribute on
    the `services` package to a NEW module object, and monkeypatch restores
    `sys.modules` but not that attribute. #1609 then reaches the module via
    `import services.proactive_message_service as pms` (which resolves through
    the package attribute) while its service instance comes from the original
    module — so its patch lands on the wrong object and its assertions fail.

    Caught by CI's base-vs-head regression diff, not by local runs: the two files
    have to execute in that order in one session for it to show.
    """
    import services.proactive_message_service as pms

    fake_db = MagicMock()
    fake_db.get_or_create_public_chat_session.return_value = {"id": "sess-1"}
    monkeypatch.setattr(pms, "db", fake_db)
    monkeypatch.setattr(pms, "platform_audit_service", MagicMock(log=AsyncMock()))

    from services.proactive_message_service import ProactiveMessageService, DeliveryResult

    service = ProactiveMessageService()
    # Rate limiting reaches for Redis via `routers.auth`, which drags in the whole
    # router import chain. Stub both: an ImportError here would otherwise be
    # swallowed by _send_message_inner's per-channel `except Exception` and
    # resurface as RecipientNotFoundError — making a test pass for the wrong
    # reason rather than fail loudly.
    service._check_rate_limit = lambda *a, **kw: True
    service._increment_rate_limit = lambda *a, **kw: None
    service._get_redis = lambda: None

    return types.SimpleNamespace(
        service=service,
        db=fake_db,
        DeliveryResult=DeliveryResult,
    )


class TestPersistOutbound:
    def test_persists_assistant_turn_with_903_attribution(self, svc):
        """role=assistant, sender_label=agent, sender_email=recipient.

        A proactive send always targets ONE verified email — a DM, i.e. a
        single-participant session — so `_assistant_sender_email`'s rule resolves
        to the recipient. That keeps sender-filtered MEM-001 summarization
        folding it into the right user's memory (AC-7).
        """
        result = svc.DeliveryResult(
            success=True, channel="telegram", session_identifier="bot:1:1"
        )
        svc.service._persist_outbound("agent-a", "user@example.com", "ping", result)

        svc.db.get_or_create_public_chat_session.assert_called_once_with(
            "agent-a", "bot:1:1", "telegram"
        )
        args, kwargs = svc.db.add_public_chat_message.call_args
        assert args[0] == "sess-1"
        assert args[1] == "assistant"
        assert args[2] == "ping"
        assert kwargs["sender_email"] == "user@example.com"
        assert kwargs["sender_label"] == "agent-a"

    def test_no_session_identifier_skips_persistence_without_crashing(self, svc):
        """AC-6: a channel that can't resolve a session still delivers; it must
        not raise and must not write a session-less turn."""
        result = svc.DeliveryResult(success=True, channel="web", session_identifier=None)
        svc.service._persist_outbound("agent-a", "user@example.com", "ping", result)
        svc.db.add_public_chat_message.assert_not_called()
        svc.db.get_or_create_public_chat_session.assert_not_called()

    def test_persistence_failure_never_breaks_delivery(self, svc):
        """The message is already sent — a DB error must not surface as a
        delivery failure or an exception."""
        svc.db.get_or_create_public_chat_session.side_effect = RuntimeError("db down")
        result = svc.DeliveryResult(
            success=True, channel="telegram", session_identifier="bot:1:1"
        )
        svc.service._persist_outbound("agent-a", "user@example.com", "ping", result)  # no raise

    def test_session_is_created_when_absent(self, svc):
        """The chat-link `session_id` column the issue proposed keying off is
        never written by any code path, so 'reuse the existing session' isn't
        available — get_or_create is what lands us in the router's session."""
        result = svc.DeliveryResult(
            success=True, channel="telegram", session_identifier="bot:1:1"
        )
        svc.service._persist_outbound("agent-a", "user@example.com", "ping", result)
        assert svc.db.get_or_create_public_chat_session.called

    def test_session_object_with_attribute_id_is_supported(self, svc):
        """get_or_create returns a model on some paths and a dict on others —
        the router handles both (`session.id if hasattr(...)`); so must we."""
        svc.db.get_or_create_public_chat_session.return_value = types.SimpleNamespace(id="sess-obj")
        result = svc.DeliveryResult(
            success=True, channel="slack", session_identifier="T1:U9:D5"
        )
        svc.service._persist_outbound("agent-a", "user@example.com", "ping", result)
        assert svc.db.add_public_chat_message.call_args[0][0] == "sess-obj"


class TestPersistOnlyOnSuccess:
    @pytest.mark.asyncio
    async def test_failed_delivery_writes_no_phantom_turn(self, svc, monkeypatch):
        """AC-5: a failed send must not leave an assistant turn in history."""
        async def _fail(*a, **kw):
            return svc.DeliveryResult(
                success=False, channel="telegram", error="boom",
                session_identifier="bot:1:1",
            )
        monkeypatch.setattr(svc.service, "_deliver_via_channel", _fail)

        from services.proactive_message_service import RecipientNotFoundError
        with pytest.raises(RecipientNotFoundError):
            await svc.service._send_message_inner(
                "agent-a", "user@example.com", "ping", "telegram", False,
            )
        svc.db.add_public_chat_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_delivery_persists(self, svc, monkeypatch):
        async def _ok(*a, **kw):
            return svc.DeliveryResult(
                success=True, channel="telegram", session_identifier="bot:1:1"
            )
        monkeypatch.setattr(svc.service, "_deliver_via_channel", _ok)

        await svc.service._send_message_inner(
            "agent-a", "user@example.com", "ping", "telegram", False,
        )
        assert svc.db.add_public_chat_message.called

    @pytest.mark.asyncio
    async def test_access_grant_notification_does_not_persist(self, svc, monkeypatch):
        """Out of scope (#951). The `_deliver_*` helpers are shared, so this
        guards the boundary: persistence hangs off the send_message path only.
        """
        async def _ok(*a, **kw):
            return svc.DeliveryResult(
                success=True, channel="telegram", session_identifier="bot:1:1"
            )
        monkeypatch.setattr(svc.service, "_deliver_via_channel", _ok)

        await svc.service.send_access_grant_notification(
            "agent-a", "user@example.com", "telegram", "you're in",
        )
        svc.db.add_public_chat_message.assert_not_called()
