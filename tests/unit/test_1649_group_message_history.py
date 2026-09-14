"""#1649 — proactive GROUP messages are persisted to channel session history.

The gap: `send_group_message` (#349/#350) delivered a broadcast and wrote nothing
to `public_chat_messages`, so the agent had no record of its own outreach.

This is the group counterpart of #1600 (DMs), but the session model differs per
channel and the outcomes are NOT equivalent:

* **Slack — real recall fix.** Channel sessions are thread-scoped
  (`team:channel:thread`, #903) and a reply carries `thread_ts` = the parent's
  ts. Capturing the posted message's own ts lands the broadcast in exactly the
  session an in-thread reply resolves to. `test_slack_key_matches_inbound_thread_reply`
  is that claim.
* **Telegram — bookkeeping only.** Group sessions are keyed per *(sender, chat)*
  (the adapter has no group branch) and a broadcast has no human sender, so it is
  filed under a synthetic agent-sender key that nothing else writes to. The agent
  still will NOT recall it. `test_telegram_key_deliberately_does_not_match_a_participant`
  pins that as intended-and-known, so a future reader doesn't mistake it for a bug
  — and so that if someone later adds a group branch to the adapter, the test
  fails and forces the decision.

Lesson carried from #1600: assert the key the PRODUCTION path derives, not one
re-built inside the test. There, a suite of probe-vs-probe comparisons passed
over a sabotaged service.

Issue: https://github.com/abilityai/trinity/issues/1649
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_backend = str(Path(__file__).resolve().parents[2] / "src" / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Session keys — derived by the production helpers the routers call
# --------------------------------------------------------------------------- #
class TestSessionKeys:
    def test_slack_key_matches_inbound_thread_reply(self):
        """THE claim for Slack: the broadcast lands where replies land.

        Compares the helper the router calls against the adapter's own key for a
        real inbound in-thread reply. If these ever diverge, the agent stops
        recalling its broadcast and nothing else would notice.
        """
        from adapters.base import NormalizedMessage
        from adapters.slack_adapter import SlackAdapter
        from services import channel_history

        posted_ts = "1720000000.111222"
        derived = channel_history.session_key_for_slack_channel(
            team_id="T1", channel_id="C1", thread_ts=posted_ts
        )
        inbound_reply = SlackAdapter().get_session_identifier(NormalizedMessage(
            sender_id="U9", text="yes, let's do it", channel_id="C1",
            thread_id=posted_ts, timestamp="t",
            metadata={"team_id": "T1", "is_dm": False},
        ))
        assert derived == inbound_reply, (
            f"broadcast filed at {derived!r} but an in-thread reply resolves to "
            f"{inbound_reply!r} — the agent would not recall its own message"
        )
        assert derived == "T1:C1:1720000000.111222"

    def test_telegram_key_deliberately_does_not_match_a_participant(self):
        """Pins the KNOWN limitation (see module docstring).

        Telegram group sessions are per-(sender, chat); the broadcast uses a
        synthetic agent-sender key, so no participant's session contains it. This
        asserts the accepted trade-off rather than a fixed bug — and fails loudly
        if the adapter later grows a group branch, forcing a re-decision.
        """
        from adapters.base import NormalizedMessage
        from adapters.telegram_adapter import TelegramAdapter
        from services import channel_history

        derived = channel_history.session_key_for_telegram_group(
            bot_id="bot-7", sender_id="atlas", chat_id="-100999"
        )
        participant_inbound = TelegramAdapter().get_session_identifier(NormalizedMessage(
            sender_id="55555", text="yes", channel_id="-100999", timestamp="t",
            metadata={"bot_id": "bot-7", "is_group": True},
        ))
        assert derived == "bot-7:atlas:-100999"
        assert derived != participant_inbound, (
            "Telegram group keys now collide with a participant session — if the "
            "adapter gained a group branch, revisit #1649's session model"
        )


# --------------------------------------------------------------------------- #
# Persistence behaviour
# --------------------------------------------------------------------------- #
@pytest.fixture
def hist(monkeypatch):
    """channel_history with its module-level `db` stubbed.

    Patches the bound name rather than stubbing sys.modules + re-importing —
    that approach poisoned a neighbouring suite in #1600 (re-import rebinds the
    package attribute; monkeypatch restores sys.modules but not that).
    """
    from services import channel_history as ch

    fake_db = MagicMock()
    fake_db.get_or_create_public_chat_session.return_value = {"id": "sess-1"}
    monkeypatch.setattr(ch, "db", fake_db)
    return types.SimpleNamespace(mod=ch, db=fake_db)


class TestPersistOutboundGroupMessage:
    def test_persists_with_903_shared_thread_attribution(self, hist):
        """sender_email MUST be None for a group/channel session.

        This is the key difference from the DM case (#1600 stamps the
        recipient's email): a shared thread's reply must never be folded into one
        participant's durable MEM-001 memory (#903 `_assistant_sender_email`).
        """
        hist.mod.persist_outbound_group_message(
            agent_name="atlas", channel="slack",
            session_identifier="T1:C1:111.222", text="deploy at 4pm",
        )
        hist.db.get_or_create_public_chat_session.assert_called_once_with(
            "atlas", "T1:C1:111.222", "slack"
        )
        args, kwargs = hist.db.add_public_chat_message.call_args
        assert args[0] == "sess-1"
        assert args[1] == "assistant"
        assert args[2] == "deploy at 4pm"
        assert kwargs["sender_email"] is None, "a broadcast must not enter one user's memory"
        assert kwargs["sender_label"] == "atlas"

    def test_no_key_skips_without_crashing(self, hist):
        hist.mod.persist_outbound_group_message(
            agent_name="atlas", channel="slack", session_identifier=None, text="hi",
        )
        hist.db.add_public_chat_message.assert_not_called()

    def test_persistence_failure_never_breaks_delivery(self, hist):
        """The message is already sent — a DB error must not raise."""
        hist.db.get_or_create_public_chat_session.side_effect = RuntimeError("db down")
        hist.mod.persist_outbound_group_message(
            agent_name="atlas", channel="telegram",
            session_identifier="bot-7:atlas:-100999", text="hi",
        )  # no raise

    def test_session_object_with_attribute_id_supported(self, hist):
        hist.db.get_or_create_public_chat_session.return_value = types.SimpleNamespace(id="sess-obj")
        hist.mod.persist_outbound_group_message(
            agent_name="atlas", channel="slack",
            session_identifier="T1:C1:111.222", text="hi",
        )
        assert hist.db.add_public_chat_message.call_args[0][0] == "sess-obj"


# --------------------------------------------------------------------------- #
# The routers — do they actually persist, with the right key?
# --------------------------------------------------------------------------- #
class TestRoutersPersist:
    """Drive the real endpoint functions.

    The helper tests above pass even if a router never calls them: deleting the
    Slack router's persistence block outright left all 9 of them green. These
    call the endpoints directly (plain async functions; auth is FastAPI's job,
    not this test's) and assert what the router hands the persistence layer.
    """

    @pytest.mark.asyncio
    async def test_slack_router_persists_at_the_posted_ts(self, monkeypatch):
        from routers import slack as slack_router

        captured = {}

        def _persist(agent_name, channel, session_identifier, text):
            captured.update(locals())

        monkeypatch.setattr(slack_router.channel_history,
                            "persist_outbound_group_message", _persist)
        monkeypatch.setattr(slack_router.db, "can_user_share_agent", lambda *a: True)
        monkeypatch.setattr(slack_router.db, "get_slack_channels_for_agent",
                            lambda *a: [{"slack_channel_id": "C1", "team_id": "T1",
                                         "slack_channel_name": "general",
                                         # ent#223: a CONSENTED channel. Proactive posts now
                                         # require allow_proactive; these tests exercise the
                                         # SEND path, not the consent gate.
                                         "allow_proactive": True}])
        monkeypatch.setattr(slack_router.db, "get_slack_workspace_bot_token", lambda *a: "xoxb-x")
        monkeypatch.setattr(slack_router, "get_proactive_rate_limit", lambda *a: 0)

        async def _send(**kw):
            return True, None, "1720000000.111222"
        monkeypatch.setattr(slack_router.slack_service, "send_message_detailed", _send)

        req = types.SimpleNamespace(message="deploy at 4pm", thread_ts=None)
        # connector_agent=None, mcp_scope=None: the migrated owner gate (assert_agent_owner →
        # _enforce_connector_scope, #1710) reads this; a real non-connector
        # User carries it. can_user_share_agent stays stubbed True, so the owner
        # is still admitted exactly as before the migration.
        user = types.SimpleNamespace(username="admin", connector_agent=None, mcp_scope=None)
        await slack_router.send_agent_slack_channel_message("atlas", "C1", req, user)

        assert captured, "the router never persisted the broadcast"
        assert captured["session_identifier"] == "T1:C1:1720000000.111222", (
            "top-level post must be filed at its OWN ts — the key an in-thread "
            "reply will resolve to"
        )
        assert captured["channel"] == "slack"
        assert captured["agent_name"] == "atlas"
        assert captured["text"] == "deploy at 4pm"

    @pytest.mark.asyncio
    async def test_slack_router_uses_the_parent_thread_when_replying(self, monkeypatch):
        """Replying into an existing thread keys on THAT thread, not the reply's ts."""
        from routers import slack as slack_router

        captured = {}
        monkeypatch.setattr(slack_router.channel_history, "persist_outbound_group_message",
                            lambda **kw: captured.update(kw))
        monkeypatch.setattr(slack_router.db, "can_user_share_agent", lambda *a: True)
        monkeypatch.setattr(slack_router.db, "get_slack_channels_for_agent",
                            lambda *a: [{"slack_channel_id": "C1", "team_id": "T1",
                                         "allow_proactive": True}])  # ent#223: consented
        monkeypatch.setattr(slack_router.db, "get_slack_workspace_bot_token", lambda *a: "xoxb-x")
        monkeypatch.setattr(slack_router, "get_proactive_rate_limit", lambda *a: 0)

        async def _send(**kw):
            return True, None, "9999.0000"  # the reply's own ts — must NOT be the key
        monkeypatch.setattr(slack_router.slack_service, "send_message_detailed", _send)

        req = types.SimpleNamespace(message="ack", thread_ts="1720000000.111222")
        # connector_agent=None, mcp_scope=None: the migrated owner gate (assert_agent_owner →
        # _enforce_connector_scope, #1710) reads this; a real non-connector
        # User carries it. can_user_share_agent stays stubbed True, so the owner
        # is still admitted exactly as before the migration.
        user = types.SimpleNamespace(username="admin", connector_agent=None, mcp_scope=None)
        await slack_router.send_agent_slack_channel_message("atlas", "C1", req, user)

        assert captured["session_identifier"] == "T1:C1:1720000000.111222"

    @pytest.mark.asyncio
    async def test_telegram_router_persists_under_the_synthetic_agent_key(self, monkeypatch):
        """The router must file the broadcast under the agent-as-sender key —
        proving it passes `agent_name` as sender_id, not a real user."""
        from routers import telegram as tg_router

        captured = {}
        monkeypatch.setattr(tg_router.channel_history, "persist_outbound_group_message",
                            lambda **kw: captured.update(kw))
        monkeypatch.setattr(tg_router.db, "get_telegram_binding",
                            lambda *a: {"id": 1, "bot_id": "bot-7"})
        monkeypatch.setattr(tg_router.db, "get_telegram_groups_for_agent",
                            lambda *a: [{"chat_id": "-100999", "is_active": True,
                                         "chat_title": "Ops"}])
        monkeypatch.setattr(tg_router.db, "get_telegram_bot_token", lambda *a: "tg-token")
        monkeypatch.setattr(tg_router, "get_proactive_rate_limit", lambda *a: 0)

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"result": {"message_id": 5}}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                return _Resp()

        monkeypatch.setattr(tg_router.httpx, "AsyncClient", lambda **kw: _Client())

        req = types.SimpleNamespace(message="standup in 5")
        await tg_router.send_telegram_group_message("atlas", "-100999", req)

        assert captured, "the router never persisted the broadcast"
        assert captured["session_identifier"] == "bot-7:atlas:-100999", (
            "expected the synthetic agent-sender key (#1649)"
        )
        assert captured["channel"] == "telegram"
        assert captured["text"] == "standup in 5"

    @pytest.mark.asyncio
    async def test_slack_router_does_not_persist_a_failed_send(self, monkeypatch):
        from fastapi import HTTPException
        from routers import slack as slack_router

        called = []
        monkeypatch.setattr(slack_router.channel_history, "persist_outbound_group_message",
                            lambda **kw: called.append(kw))
        monkeypatch.setattr(slack_router.db, "can_user_share_agent", lambda *a: True)
        monkeypatch.setattr(slack_router.db, "get_slack_channels_for_agent",
                            lambda *a: [{"slack_channel_id": "C1", "team_id": "T1",
                                         "allow_proactive": True}])  # ent#223: consented
        monkeypatch.setattr(slack_router.db, "get_slack_workspace_bot_token", lambda *a: "xoxb-x")
        monkeypatch.setattr(slack_router, "get_proactive_rate_limit", lambda *a: 0)

        async def _send(**kw):
            return False, "channel_not_found", None
        monkeypatch.setattr(slack_router.slack_service, "send_message_detailed", _send)

        req = types.SimpleNamespace(message="hi", thread_ts=None)
        # connector_agent=None, mcp_scope=None: the migrated owner gate (assert_agent_owner →
        # _enforce_connector_scope, #1710) reads this; a real non-connector
        # User carries it. can_user_share_agent stays stubbed True, so the owner
        # is still admitted exactly as before the migration.
        user = types.SimpleNamespace(username="admin", connector_agent=None, mcp_scope=None)
        with pytest.raises(HTTPException):
            await slack_router.send_agent_slack_channel_message("atlas", "C1", req, user)
        assert not called, "a failed send wrote a phantom assistant turn"


# --------------------------------------------------------------------------- #
# slack_service.send_message_detailed — the ts capture the Slack fix needs
# --------------------------------------------------------------------------- #
class TestSendMessageDetailed:
    @pytest.mark.asyncio
    async def test_detailed_returns_posted_ts(self, monkeypatch):
        from services.slack_service import slack_service

        class _Resp:
            @staticmethod
            def json():
                return {"ok": True, "ts": "1720000000.111222"}

        async def _post(*a, **kw):
            return _Resp()

        monkeypatch.setattr(slack_service.client, "post", _post)
        ok, err, ts = await slack_service.send_message_detailed(
            bot_token="xoxb-x", channel="C1", text="hi"
        )
        assert (ok, err) == (True, None)
        assert ts == "1720000000.111222", "ts dropped — the Slack session key can't be derived"

    @pytest.mark.asyncio
    async def test_send_message_keeps_its_two_tuple_contract(self, monkeypatch):
        """~7 existing call sites unpack (ok, error). Adding ts must not break them."""
        from services.slack_service import slack_service

        class _Resp:
            @staticmethod
            def json():
                return {"ok": True, "ts": "1.2"}

        async def _post(*a, **kw):
            return _Resp()

        monkeypatch.setattr(slack_service.client, "post", _post)
        result = await slack_service.send_message(bot_token="xoxb-x", channel="C1", text="hi")
        assert result == (True, None)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_detailed_reports_error_and_no_ts_on_failure(self, monkeypatch):
        from services.slack_service import slack_service

        class _Resp:
            @staticmethod
            def json():
                return {"ok": False, "error": "channel_not_found"}

        async def _post(*a, **kw):
            return _Resp()

        monkeypatch.setattr(slack_service.client, "post", _post)
        ok, err, ts = await slack_service.send_message_detailed(
            bot_token="xoxb-x", channel="C1", text="hi"
        )
        assert ok is False
        assert err == "channel_not_found"
        assert ts is None
