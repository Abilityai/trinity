"""An agent can message a Workspace-only person (abilityai/trinity-enterprise#1221).

`send_message(channel="web")` used to be a stub that always refused, and `auto`
discarded every channel's reason, so a member who works only in the Workspace
was unreachable and the agent was told nothing about why. These tests drive the
real proactive service over the unit island's tmp database:

* `web` lands one assistant row, marked ``source="proactive"``, in the person's
  Main conversation with the agent — through the same reachability gate a
  scheduled Workspace delivery uses;
* a retry after a post-commit failure writes nothing new (deterministic id);
* only the agent's own key may write there, and never from inside the
  recipient's own Main turn;
* every refusal carries a per-channel reason, through the route as well;
* the reachability answer the Execution Context uses agrees with the gates the
  send applies.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

pytestmark = pytest.mark.unit

EMAIL = "member@example.com"


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _rows(agent: str):
    from db.engine import get_engine

    with get_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.exec_driver_sql(
            "SELECT * FROM enterprise_portal_messages WHERE agent_name = ?", (agent,))]


@pytest.fixture
def svc(monkeypatch):
    """The real service, its non-Workspace collaborators stubbed: consent
    granted, no Telegram link, no Slack workspace, no WhatsApp binding. The
    Workspace roster is stubbed per test (shares live in tables this island
    does not seed); the block list, Main and the row writer are real."""
    from unittest.mock import MagicMock

    from client_portal import service as portal_service
    from services import proactive_message_service as pms

    svc_db = MagicMock()
    svc_db.can_agent_message_email.return_value = True
    svc_db.get_telegram_binding.return_value = {"id": 1}
    svc_db.get_telegram_chat_link_by_verified_email.return_value = None
    svc_db.get_all_slack_workspaces.return_value = []
    svc_db.get_whatsapp_binding.return_value = None
    svc_db.is_channel_client_blocked.return_value = False
    svc_db.get_execution.return_value = None
    monkeypatch.setattr(pms, "db", svc_db)
    monkeypatch.setattr(pms.proactive_message_service, "check_rate_limit", lambda *a: True)
    monkeypatch.setattr(pms.proactive_message_service, "increment_rate_limit", lambda *a: None)

    async def _no_audit(*a, **k):
        return None

    monkeypatch.setattr(pms.proactive_message_service, "audit_send", _no_audit)
    roster = {"on": True}
    monkeypatch.setattr(portal_service, "agent_on_roster", lambda a, e, inc=False: roster["on"])
    return pms, svc_db, roster


def _send(pms, agent, **kw):
    kw.setdefault("channel", "web")
    kw.setdefault("as_agent_key", True)
    return asyncio.run(pms.proactive_message_service.send_message(
        agent_name=agent, recipient_email=EMAIL, text="Your brief is ready.", **kw))


class TestWebLandsInTheWorkspace:
    def test_web_writes_one_proactive_row_into_main(self, svc):
        from client_portal import service as portal_service

        pms, _, _ = svc
        agent = _uniq("agent")
        result = _send(pms, agent)

        assert result.success is True and result.channel == "web"
        assert result.delivery == "workspace_inbox"
        rows = _rows(agent)
        assert len(rows) == 1
        row = rows[0]
        assert row["role"] == "assistant" and row["source"] == "proactive"
        assert row["content"] == "Your brief is ready."
        assert row["client_email"] == EMAIL
        assert row["session_id"] == portal_service.ensure_main_session(agent, EMAIL)
        assert row["id"] == result.message_id

    def test_auto_falls_through_to_the_workspace_when_no_other_channel_exists(self, svc):
        pms, _, _ = svc
        agent = _uniq("agent")
        result = _send(pms, agent, channel="auto")
        assert result.success is True and result.channel == "web"
        assert len(_rows(agent)) == 1

    def test_the_web_row_is_not_written_twice_into_public_chat(self, svc):
        """The Workspace row IS the history; a public_chat twin is a session
        nobody reads."""
        pms, svc_db, _ = svc
        _send(pms, _uniq("agent"))
        svc_db.get_or_create_public_chat_session.assert_not_called()
        svc_db.add_public_chat_message.assert_not_called()

    def test_a_retry_after_a_post_commit_failure_writes_nothing_new(self, svc, monkeypatch):
        """The row commits, then the audit raises: the effect guard releases its
        claim and the turn retries. The deterministic id makes the retry a no-op
        on the row instead of a second visible message."""
        pms, _, _ = svc
        agent = _uniq("agent")
        calls = {"n": 0}

        async def _audit_fails_once(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("audit down")

        monkeypatch.setattr(pms.proactive_message_service, "audit_send", _audit_fails_once)
        exe = _uniq("exe")
        with pytest.raises(Exception):  # the send loop reports it as a refusal
            _send(pms, agent, execution_id=exe)
        assert len(_rows(agent)) == 1  # the row DID commit before the raise
        result = _send(pms, agent, execution_id=exe)
        assert result.success is True
        assert len(_rows(agent)) == 1

    def test_two_labelled_sends_in_one_turn_are_two_rows(self, svc):
        pms, _, _ = svc
        agent = _uniq("agent")
        exe = _uniq("exe")
        _send(pms, agent, execution_id=exe, dedup_label="a")
        _send(pms, agent, execution_id=exe, dedup_label="b")
        assert len(_rows(agent)) == 2

    def test_auto_then_web_in_one_turn_are_two_rows(self, svc):
        """`auto` and `web` are two effects to the guard (the requested channel
        is in its identity), so they must be two rows: a shared row id would
        let the second send report success while writing nothing."""
        pms, _, _ = svc
        agent = _uniq("agent")
        exe = _uniq("exe")
        first = _send(pms, agent, execution_id=exe, channel="auto")
        second = _send(pms, agent, execution_id=exe, channel="web")
        assert first.success is True and second.success is True
        assert first.message_id != second.message_id
        assert len(_rows(agent)) == 2

    def test_a_suppressed_repeat_writes_no_second_row_and_no_note(self, svc):
        """An intent-key repeat (ent#665) of a web send is suppressed. The web
        result records no public_chat session, so no "Not sent" note is written
        anywhere — accepted: the person sees the one message, never a note about
        a repeat they could not see."""
        pms, svc_db, _ = svc
        agent = _uniq("agent")
        key = _uniq("brief")
        first = _send(pms, agent, execution_id=_uniq("exe"), idempotency_key=key)
        again = _send(pms, agent, execution_id=_uniq("exe"), idempotency_key=key)
        assert first.success is True and first.channel == "web"
        assert again.success is True and again.suppressed_by
        assert len(_rows(agent)) == 1
        svc_db.get_or_create_public_chat_session.assert_not_called()
        svc_db.add_public_chat_message.assert_not_called()


class TestWebRefusals:
    def _refused(self, pms, agent, **kw):
        with pytest.raises(pms.RecipientNotFoundError) as ei:
            _send(pms, agent, **kw)
        return ei.value

    def test_a_person_not_on_the_agents_roster_is_refused_by_name(self, svc):
        pms, _, roster = svc
        roster["on"] = False
        agent = _uniq("agent")
        e = self._refused(pms, agent)
        assert e.channels == {"web": "workspace_not_shared"}
        assert _rows(agent) == []

    def test_a_blocked_person_is_refused(self, svc, monkeypatch):
        from client_portal import db as portal_db

        pms, _, _ = svc
        monkeypatch.setattr(portal_db, "is_client_blocked", lambda e: True)
        e = self._refused(pms, _uniq("agent"))
        assert e.channels == {"web": "workspace_blocked"}

    def test_an_unreadable_block_list_fails_closed(self, svc, monkeypatch):
        from client_portal import db as portal_db

        pms, _, _ = svc

        def _boom(e):
            raise RuntimeError("db down")

        monkeypatch.setattr(portal_db, "is_client_blocked", _boom)
        agent = _uniq("agent")
        e = self._refused(pms, agent)
        assert e.channels == {"web": "workspace_unverifiable"}
        assert _rows(agent) == []

    def test_only_the_agents_own_key_may_write_into_a_workspace(self, svc):
        pms, _, _ = svc
        agent = _uniq("agent")
        e = self._refused(pms, agent, as_agent_key=False)
        assert e.channels == {"web": "web_agent_only"}
        e = self._refused(pms, agent, channel="auto", as_agent_key=False)
        assert e.channels["web"] == "web_agent_only"
        assert _rows(agent) == []

    def test_a_send_from_inside_the_recipients_own_main_turn_is_refused(self, svc):
        from types import SimpleNamespace

        from client_portal import service as portal_service

        pms, svc_db, _ = svc
        agent = _uniq("agent")
        main = portal_service.ensure_main_session(agent, EMAIL)
        svc_db.get_execution.return_value = SimpleNamespace(
            source_channel="portal", source_channel_chat_id=main)
        e = self._refused(pms, agent, execution_id=_uniq("exe"))
        assert e.channels == {"web": "workspace_in_turn"}
        assert _rows(agent) == []

    def test_auto_names_every_channels_reason(self, svc):
        pms, svc_db, roster = svc
        roster["on"] = False
        e = self._refused(pms, _uniq("agent"), channel="auto")
        assert e.reason == "no_channel"
        assert e.channels == {"telegram": "telegram_not_linked",
                              "slack": "slack_not_connected",
                              "web": "workspace_not_shared"}

    def test_a_channel_block_is_a_named_code(self, svc):
        pms, svc_db, roster = svc
        roster["on"] = False
        svc_db.get_telegram_chat_link_by_verified_email.return_value = {"telegram_user_id": 7}
        svc_db.is_channel_client_blocked.return_value = True
        e = self._refused(pms, _uniq("agent"), channel="auto")
        assert e.channels["telegram"] == "telegram_blocked"


class TestReachability:
    """`reachable_channels` is what the Execution Context asks; it must agree
    with the gates `send_message` applies — checked by sending, not by
    restating the rule (learnings 2026-09-07)."""

    @pytest.mark.parametrize("on_roster,blocked,linked,slack", [
        (True, False, False, False),
        (False, False, False, False),
        (True, True, False, False),
        (False, False, True, False),
        (False, False, False, True),
    ])
    def test_reachable_channels_agrees_with_the_send(self, svc, monkeypatch,
                                                     on_roster, blocked, linked, slack):
        from client_portal import db as portal_db

        pms, svc_db, roster = svc
        roster["on"] = on_roster
        monkeypatch.setattr(portal_db, "is_client_blocked", lambda e: blocked)
        if linked:
            svc_db.get_telegram_chat_link_by_verified_email.return_value = {"telegram_user_id": 7}
            svc_db.get_telegram_bot_token.return_value = None  # delivery fails after the gate
        if slack:
            svc_db.get_all_slack_workspaces.return_value = [{"bot_token": "x"}]

        agent = _uniq("agent")
        reachable = pms.reachable_channels(agent, EMAIL)
        assert ("web" in reachable) == (on_roster and not blocked)
        assert ("telegram" in reachable) == linked
        assert ("slack" in reachable) == slack
        # The send's own verdict on web, for the same state:
        if "web" in reachable:
            assert _send(pms, agent).success is True
        else:
            with pytest.raises(pms.RecipientNotFoundError):
                _send(pms, agent)

    def test_reachability_writes_nothing(self, svc):
        pms, _, _ = svc
        agent = _uniq("agent")
        assert "web" in pms.reachable_channels(agent, EMAIL)
        from db.engine import get_engine

        with get_engine().connect() as conn:
            n = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM enterprise_portal_sessions WHERE agent_name = ?",
                (agent,)).scalar()
        assert n == 0


class TestTheAgentSeesItNextTurn:
    def test_a_proactive_row_is_told_as_a_between_turns_message(self):
        from client_portal import service as portal_service

        lines = portal_service._context_lines(
            [{"role": "assistant", "content": "Your brief is ready.", "source": "proactive"}],
            10_000)
        assert lines == ["[You messaged them between turns: Your brief is ready.]"]
