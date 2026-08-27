"""Telegram completion report-back (ent#265) — the Telegram edition of ent#224.

A Telegram-triggered long/delegated task posts its terminal back to the
originating chat, threaded to the triggering message. Everything is a JOIN of
shipped parts; these tests pin the new joints:

* **D0** — inherited channel context is persisted ON THE CHILD ROW at the /task
  row-creation point (``create_task_execution_and_activities``). The prior
  ``run_async_task`` → ``execute_task(source_channel=…)`` threading was dead
  (the pre-created ``execution_id`` skips the creation branch that writes the
  columns), which is exactly why the Slack ent#224 delegated path shipped
  broken and why the row-read-back tests here go against a REAL DB — a
  SimpleNamespace mock row cannot see a severed write path.
* **D1** — binding-agent resolution: consent + bot token evaluate against
  ``row.source_channel_agent or row.agent_name`` for BOTH channels; NULL is
  byte-identical legacy behavior.
* **D2** — Telegram consent units: group = ``is_active AND allow_proactive``
  (default allow); DM = consent-by-construction (chat link exists); unknown
  destination = suppress.
* **D3** — the ``apply_result`` failure branch spawns the report (CAS-won only).
* **D5/D6** — HTML pre-escape + post-conversion 4096 cap; numeric-thread guard.
* **D9** — effect_guard keeps ``agent_name`` = the EXECUTING agent; the replay
  test runs the REAL guard with ``source_channel_agent != agent_name`` so a
  "consistency" refactor that passes binding_agent (fail-open → dedup DISARMED)
  fails loudly.

Module: src/backend/services/channel_completion_report.py
        src/backend/services/chat_execution_service.py
        src/backend/services/task_execution_service.py
        src/backend/routers/telegram.py
Issue:  https://github.com/Abilityai/trinity-enterprise/issues/265
"""

import asyncio
import os
import secrets
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import db_backend, seed_agent, seed_user  # noqa: E402,F401

from services import channel_completion_report as ccr  # noqa: E402

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.run(coro)


def _execution(*, triggered_by="agent", channel="telegram", chat_id="-100777",
               thread="42", agent_name="worker-b", source_channel_agent=None):
    return SimpleNamespace(
        source_channel=channel,
        source_channel_chat_id=chat_id,
        source_channel_thread=thread,
        source_channel_agent=source_channel_agent,
        triggered_by=triggered_by,
        agent_name=agent_name,
    )


# ===========================================================================
# Wired-mock layer — reporter behavior (destination, consent, rendering).
# The DB here is a stub because these tests pin the reporter's DECISIONS;
# the write-path/row-read-back class below uses the real DB.
# ===========================================================================
@pytest.fixture
def wired(monkeypatch):
    """Wire collaborators; return (telegram_sends, slack_sends, fake_db)."""
    tg_sent = []
    slack_sent = []

    from adapters.telegram_adapter import TelegramAdapter

    async def _tg_send(self, bot_token, chat_id, text, reply_to_message_id=None,
                       parse_mode="HTML"):
        tg_sent.append({
            "bot_token": bot_token, "chat_id": chat_id, "text": text,
            "reply_to_message_id": reply_to_message_id, "parse_mode": parse_mode,
        })
        return {"message_id": 999}

    monkeypatch.setattr(TelegramAdapter, "_send_message", _tg_send)

    async def _slack_send(**kwargs):
        slack_sent.append(kwargs)
        return (True, None, "9999.0001")

    monkeypatch.setitem(sys.modules, "services.slack_service",
                        types.SimpleNamespace(
                            slack_service=types.SimpleNamespace(
                                send_message_detailed=_slack_send)))

    group_cfg = {"id": 5, "is_active": 1, "allow_proactive": True}
    fake_db = types.SimpleNamespace(
        get_execution=lambda eid: _execution(),
        # Telegram — keyed on the agent argument so binding-agent tests bite.
        get_telegram_binding=lambda a: {"id": 7, "agent_name": a},
        get_telegram_group_config=lambda bid, cid: dict(group_cfg),
        get_telegram_chat_link=lambda bid, uid: None,
        get_telegram_bot_token=lambda a: f"tok-{a}",
        # Slack — for the shared binding-agent parametrization.
        get_slack_channels_for_agent=lambda a: [
            {"slack_channel_id": "C123", "team_id": "T1", "allow_proactive": True}],
        get_slack_workspace_bot_token=lambda t: "xoxb-test",
    )
    monkeypatch.setitem(sys.modules, "database", types.SimpleNamespace(db=fake_db))

    class _G:
        replay = False
        snapshot = None

    @asynccontextmanager
    async def _guard(*a, **k):
        yield _G()

    monkeypatch.setitem(
        sys.modules, "services.idempotency_service",
        types.SimpleNamespace(effect_guard=_guard,
                              EffectInProgressError=type("E", (Exception,), {})))
    return tg_sent, slack_sent, fake_db


def _report(**over):
    kw = dict(execution_id="e1", agent_name="worker-b",
              status="success", summary_or_error="done")
    kw.update(over)
    return _run(ccr.report_completion(**kw))


class TestTelegramDelivery:
    def test_telegram_delegated_terminal_is_reported(self, wired):
        """A→B delegation into a consented group: B's completion reports,
        threaded to the triggering message (AC#1/#3)."""
        tg, _, _ = wired
        assert _report() is True
        assert len(tg) == 1
        assert tg[0]["chat_id"] == "-100777"
        assert tg[0]["reply_to_message_id"] == "42"
        assert tg[0]["parse_mode"] == "HTML"

    def test_telegram_dm_is_consent_by_construction(self, wired):
        """A DM chat link exists ⇒ the user cold-started this bot — no extra
        consent flag is required (D2)."""
        tg, _, db = wired
        db.get_execution = lambda eid: _execution(chat_id="12345")
        db.get_telegram_group_config = lambda bid, cid: None
        db.get_telegram_chat_link = lambda bid, uid: {"id": 1, "telegram_user_id": uid}
        assert _report() is True
        assert len(tg) == 1
        assert tg[0]["chat_id"] == "12345"

    @pytest.mark.parametrize("thread", [None, "", "abc", "1.5"])
    def test_non_numeric_thread_is_omitted_but_still_delivered(self, wired, thread):
        """D6/L2: the int() cast in _send_message sits OUTSIDE its try — a
        non-numeric or absent thread must be dropped, never crash the report."""
        tg, _, db = wired
        db.get_execution = lambda eid: _execution(thread=thread)
        assert _report() is True
        assert tg[0]["reply_to_message_id"] is None

    def test_telegram_report_threads_to_triggering_message(self, wired):
        """DMs anchor too — a report hours later needs 'which request?' (D6)."""
        tg, _, db = wired
        db.get_execution = lambda eid: _execution(chat_id="12345", thread="777")
        db.get_telegram_group_config = lambda bid, cid: None
        db.get_telegram_chat_link = lambda bid, uid: {"id": 1}
        assert _report() is True
        assert tg[0]["reply_to_message_id"] == "777"

    def test_telegram_group_without_consent_is_suppressed(self, wired):
        tg, _, db = wired
        db.get_telegram_group_config = lambda bid, cid: {
            "id": 5, "is_active": 1, "allow_proactive": False}
        assert _report() is False
        assert tg == []

    def test_telegram_inactive_group_is_suppressed(self, wired):
        """F8/L3: get_group_config does NOT filter is_active — the conjunction
        needs its own pin (consent alone must not resurrect a removed group)."""
        tg, _, db = wired
        db.get_telegram_group_config = lambda bid, cid: {
            "id": 5, "is_active": 0, "allow_proactive": True}
        assert _report() is False
        assert tg == []

    def test_telegram_unknown_destination_is_suppressed(self, wired):
        """Neither a group config nor a chat link — mirrors Slack's
        unbound-channel suppression; never fire a send destined to fail."""
        tg, _, db = wired
        db.get_telegram_group_config = lambda bid, cid: None
        db.get_telegram_chat_link = lambda bid, uid: None
        assert _report() is False
        assert tg == []

    def test_telegram_bot_cannot_post_is_graceful(self, wired, monkeypatch):
        """AC#5/D4: _send_message → None (kicked/blocked/API error) is a logged
        no-op — False, no raise, no retry."""
        from adapters.telegram_adapter import TelegramAdapter

        async def _fail(self, *a, **k):
            return None

        monkeypatch.setattr(TelegramAdapter, "_send_message", _fail)
        assert _report() is False

    def test_no_telegram_binding_for_binding_agent_is_suppressed(self, wired):
        tg, _, db = wired
        db.get_telegram_binding = lambda a: None
        assert _report() is False
        assert tg == []

    @pytest.mark.parametrize("trigger", ["slack", "telegram", "whatsapp"])
    def test_inline_telegram_turn_is_never_reported(self, wired, trigger):
        """The no-spam rule (AC#3): the adapter already replied inline."""
        tg, _, db = wired
        db.get_execution = lambda eid: _execution(triggered_by=trigger)
        assert _report() is False
        assert tg == []

    def test_telegram_failure_terminal_reports(self, wired):
        """AC#2 + D5/M2: the ⚠️ head, with the escaped `<class 'ValueError'>`
        INTACT — unescaped it trips 'can't parse entities' and the strip-HTML
        fallback deletes the exception name from the message."""
        tg, _, _ = wired
        assert _report(status="failed",
                       summary_or_error="boom: <class 'ValueError'>") is True
        text = tg[0]["text"]
        assert "⚠️" in text and "failed" in text
        assert "&lt;class 'ValueError'&gt;" in text

    def test_telegram_report_capped_after_html_conversion(self, wired):
        """D5/M2: entity escaping inflates the text (& → &amp;) — the cap must
        re-apply AFTER conversion, because 'message too long' is a 400 the
        parse-failure fallback does not catch (silent loss)."""
        tg, _, _ = wired
        assert _report(summary_or_error="&" * 2500) is True
        text = tg[0]["text"]
        assert len(text) <= 4096
        assert text.endswith("…")

    def test_telegram_attribution_when_delegated(self, wired):
        """D1c: Telegram has no per-message sender name — when the report
        arrives via A's bot but B did the work, the head names B."""
        tg, _, db = wired
        db.get_execution = lambda eid: _execution(
            agent_name="worker-b", source_channel_agent="agent-a")
        assert _report() is True
        assert "— worker-b" in tg[0]["text"].splitlines()[0]
        # …and no attribution suffix when binding == executing agent:
        tg.clear()
        db.get_execution = lambda eid: _execution(agent_name="worker-b")
        assert _report() is True
        assert "—" not in tg[0]["text"].splitlines()[0]


class TestBindingAgentResolution:
    def test_telegram_binding_resolution_prefers_source_channel_agent(self, wired):
        """D1: A's binding + token deliver when the row carries A."""
        tg, _, db = wired
        seen = []
        db.get_execution = lambda eid: _execution(
            agent_name="worker-b", source_channel_agent="agent-a")
        db.get_telegram_binding = lambda a: (seen.append(a) or {"id": 7})
        assert _report() is True
        assert seen == ["agent-a"]
        assert tg[0]["bot_token"] == "tok-agent-a"

    def test_slack_binding_resolution_prefers_source_channel_agent(self, wired):
        """D1 is channel-agnostic: Slack consent + token key on A too, while
        the displayed identity stays the EXECUTING agent (D1b attribution)."""
        _, slack, db = wired
        seen = []
        db.get_execution = lambda eid: _execution(
            channel="slack", chat_id="C123", thread="1.5",
            agent_name="worker-b", source_channel_agent="agent-a")
        db.get_slack_channels_for_agent = lambda a: (seen.append(a) or [
            {"slack_channel_id": "C123", "team_id": "T1", "allow_proactive": True}])
        assert _report() is True
        assert seen == ["agent-a"]
        assert slack[0]["username"] == "worker-b"

    def test_null_source_channel_agent_falls_back_to_executing_agent(self, wired):
        """Legacy/pre-migration rows: NULL column ⇒ byte-identical old
        behavior (consent + token keyed on the executing agent)."""
        tg, _, db = wired
        seen = []
        db.get_execution = lambda eid: _execution(
            agent_name="worker-b", source_channel_agent=None)
        db.get_telegram_binding = lambda a: (seen.append(a) or {"id": 7})
        assert _report() is True
        assert seen == ["worker-b"]

    def test_slack_consent_narrowing_is_intentional(self, wired):
        """D1b pin: a channel that consented to worker B but NOT to originating
        A is now SUPPRESSED for rows carrying source_channel_agent=A — the user
        addressed A, so A's consent governs."""
        _, slack, db = wired
        db.get_execution = lambda eid: _execution(
            channel="slack", chat_id="C123", thread="1.5",
            agent_name="worker-b", source_channel_agent="agent-a")
        db.get_slack_channels_for_agent = lambda a: (
            [{"slack_channel_id": "C123", "team_id": "T1", "allow_proactive": True}]
            if a == "worker-b" else []
        )
        assert _report() is False
        assert slack == []


# ===========================================================================
# Chokepoint layer — the D3 failure-applier hook (mirrors test_1578's
# _run_apply harness so the CAS gating is exercised, not asserted-by-mock-db).
# ===========================================================================
def _close_coro(coro):
    try:
        coro.close()
    except Exception:
        pass


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _envelope(status_name, **over):
    from services.task_execution_service import TerminalEnvelope, TaskExecutionStatus

    status = getattr(TaskExecutionStatus, status_name)
    base = dict(execution_id="exec-265", status=status)
    if status_name == "SUCCESS":
        base.update(response="all done", metadata={"cost_usd": 0.05},
                    session_id="s", execution_time_ms=10)
    else:
        base.update(error="agent said no", error_code=None, metadata={})
    base.update(over)
    return TerminalEnvelope(**base)


def _run_apply(envelope, *, cas_won=True):
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.update_execution_status.return_value = cas_won
    mock_db.get_execution.return_value = SimpleNamespace(
        id="exec-265", status="cancelled")

    mock_ccr = MagicMock()

    with (
        patch("services.task_execution_service.db", mock_db),
        patch("services.task_execution_service.get_capacity_manager",
              return_value=MagicMock(release=AsyncMock())),
        patch("services.task_execution_service.activity_service",
              MagicMock(complete_activity=AsyncMock())),
        patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
        patch("services.task_execution_service._spawn_bg", MagicMock(side_effect=_close_coro)),
        patch("services.task_execution_service.event_dispatch_service", MagicMock()),
        patch("services.task_execution_service.channel_completion_report", mock_ccr),
    ):
        svc = TaskExecutionService()
        _await(svc.apply_result("worker-b", envelope, activity_id="act-1"))
    return mock_ccr


class TestApplyResultChokepoints:
    def test_apply_result_failure_branch_spawns_completion_report(self):
        """D3 — the gap this PR closes: the failure applier emitted the #1578
        event but never the channel report (the path agent-reported failure
        envelopes take)."""
        ccr_mock = _run_apply(_envelope("FAILED"), cas_won=True)
        ccr_mock.spawn_completion_report.assert_called_once()
        _, kwargs = ccr_mock.spawn_completion_report.call_args
        assert kwargs["status"] == "failed"
        assert kwargs["summary_or_error"] == "agent said no"

    def test_apply_result_failure_lost_cas_spawns_nothing(self):
        """A replayed/late callback loses the CAS → no side effects (#1084)."""
        ccr_mock = _run_apply(_envelope("FAILED"), cas_won=False)
        ccr_mock.spawn_completion_report.assert_not_called()

    def test_apply_result_success_branch_spawns_completion_report(self):
        """F8 chokepoint pin — the 224 suite never covered the chokepoints."""
        ccr_mock = _run_apply(_envelope("SUCCESS"), cas_won=True)
        ccr_mock.spawn_completion_report.assert_called_once()
        _, kwargs = ccr_mock.spawn_completion_report.call_args
        assert kwargs["status"] == "success"

    def test_cancelled_envelope_reports_through_failure_applier(self):
        """#679 CANCELLED envelopes finalize through the non-success applier
        and report too — uniform with _write_terminal_and_gate (L3)."""
        ccr_mock = _run_apply(
            _envelope("CANCELLED", error="cancelled by user"), cas_won=True)
        ccr_mock.spawn_completion_report.assert_called_once()
        _, kwargs = ccr_mock.spawn_completion_report.call_args
        assert kwargs["status"] == "cancelled"


# ===========================================================================
# Real-DB layer (db_harness) — D0 row read-back, transitive inheritance,
# the REAL effect_guard replay pin, fan-out semantics, live-select accessors.
# ===========================================================================
@pytest.fixture
def encryption_key(monkeypatch):
    """create_telegram_binding encrypts the bot token; needs a key."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    yield


def _decision():
    from services.idempotency_service import IdempotencyDecision

    return IdempotencyDecision(enabled=False, replay=False, in_flight=False)


def _user(user_id=1, username="owner", role="user", agent_name=None,
          connector_agent=None, mcp_scope=None):
    """A principal. ``agent_name`` set ⇒ an AGENT-scoped MCP key (scope='agent'),
    which is what the provenance guard's agent arm keys on — NOT the
    X-Source-Agent header (unvalidated client input for a human caller)."""
    from models import User

    return User(id=user_id, username=username, role=role,
                email=f"{username}@example.com", agent_name=agent_name,
                connector_agent=connector_agent, mcp_scope=mcp_scope)


def _request(message="do the thing", parent_execution_id=None):
    return SimpleNamespace(
        message=message, model=None, parent_execution_id=parent_execution_id,
        inject_result=False, chat_session_id=None,
    )


async def _create_child(*, name, current_user, x_source_agent=None, parent_id=None):
    from services import chat_execution_service as ces

    with (
        patch.object(ces, "activity_service",
                     MagicMock(track_activity=AsyncMock(return_value="act-x"))),
        patch.object(ces, "broadcast_collaboration_event", AsyncMock()),
    ):
        execution_id, _, _, _ = await ces.create_task_execution_and_activities(
            request=_request(parent_execution_id=parent_id),
            name=name,
            current_user=current_user,
            x_source_agent=x_source_agent,
            triggered_by="agent" if x_source_agent else "manual",
            is_self_task=False,
            idem=_decision(),
        )
    return execution_id


def _seed_parent(agent="agent-a", *, channel="telegram", chat_id="-100777",
                 thread="42", channel_agent=None):
    from database import db

    row = db.create_task_execution(
        agent_name=agent, message="parent turn", triggered_by=channel or "manual",
        source_channel=channel, source_channel_chat_id=chat_id,
        source_channel_thread=thread, source_channel_agent=channel_agent,
    )
    return row.id


class TestInheritedContextPersistence:
    """D0 end-to-end, against the real DB — the exact class of break that
    shipped ent#224 dead: a threaded parameter only one branch of the callee
    consumed. Mock-row tests cannot see it; the child row is READ BACK."""

    def test_inherited_context_is_persisted_on_child_row(self, db_backend):
        from database import db

        seed_user(1, "owner", role="admin")
        seed_agent("agent-a", 1)
        seed_agent("worker-b", 1)
        parent_id = _seed_parent("agent-a")

        child_id = _run(_create_child(
            name="worker-b", current_user=_user(role="admin"), parent_id=parent_id))

        child = db.get_execution(child_id)
        assert child.source_channel == "telegram"
        assert child.source_channel_chat_id == "-100777"
        assert child.source_channel_thread == "42"
        # Parent is a DIRECT channel row (NULL binding agent) → the child's
        # binding agent is the parent's own agent.
        assert child.source_channel_agent == "agent-a"

    def test_channelless_parent_yields_all_null(self, db_backend):
        """L1: never stamp a dangling binding-agent pointer on a child whose
        parent has no channel context."""
        from database import db

        seed_user(1, "owner", role="admin")
        seed_agent("agent-a", 1)
        parent_id = _seed_parent("agent-a", channel=None, chat_id=None, thread=None)

        child_id = _run(_create_child(
            name="agent-a", current_user=_user(role="admin"), parent_id=parent_id))

        child = db.get_execution(child_id)
        assert child.source_channel is None
        assert child.source_channel_chat_id is None
        assert child.source_channel_thread is None
        assert child.source_channel_agent is None

    def test_agent_caller_must_be_parents_executing_agent(self, db_backend):
        """Provenance guard, agent arm: an agent-scoped key naming someone
        ELSE's execution id gets no inheritance — its terminal cannot be routed
        into an unrelated chat."""
        from database import db

        seed_user(1, "owner", role="admin")
        seed_agent("agent-a", 1)
        seed_agent("agent-evil", 1)
        parent_id = _seed_parent("agent-a")

        child_id = _run(_create_child(
            name="agent-evil",
            current_user=_user(role="admin", agent_name="agent-evil"),
            x_source_agent="agent-evil", parent_id=parent_id))

        child = db.get_execution(child_id)
        assert child.source_channel is None
        assert child.source_channel_agent is None

    def test_matching_agent_caller_inherits(self, db_backend):
        """…while the parent's own executing agent DOES inherit (the A→B
        delegation shape: A calls /task on B with A's parent id)."""
        from database import db

        seed_user(1, "owner", role="admin")
        seed_agent("agent-a", 1)
        seed_agent("worker-b", 1)
        parent_id = _seed_parent("agent-a")

        child_id = _run(_create_child(
            name="worker-b",
            current_user=_user(role="admin", agent_name="agent-a"),
            x_source_agent="agent-a", parent_id=parent_id))

        child = db.get_execution(child_id)
        assert child.source_channel == "telegram"
        assert child.source_channel_agent == "agent-a"

    def test_human_caller_without_access_gets_no_inheritance(self, db_backend):
        """Provenance guard, human arm: no access to the parent's agent → no
        inheritance (fail-open to no-context, never to someone else's chat)."""
        from database import db

        seed_user(1, "owner", role="user")
        seed_user(2, "stranger", role="user")
        seed_agent("agent-a", 1)
        parent_id = _seed_parent("agent-a")

        child_id = _run(_create_child(
            name="agent-a", current_user=_user(2, "stranger"), parent_id=parent_id))

        child = db.get_execution(child_id)
        assert child.source_channel is None
        assert child.source_channel_agent is None

    def test_spoofed_source_agent_header_does_not_bypass_the_human_arm(
            self, db_backend):
        """The arm is chosen by the AUTHENTICATED PRINCIPAL, never the raw
        X-Source-Agent header. The SELF-EXEC-001 spoof guard only fires when
        ``current_user.agent_name`` is set (routers/chat.py documents the same
        trap for the resume IDOR), so a human caller can put ANY value in that
        header. Selecting the agent arm on it made the human arm a no-op: name
        the parent's own agent — which the row itself tells you — and the check
        passes trivially, routing this task's terminal into that agent's chat."""
        from database import db

        seed_user(1, "owner", role="user")
        seed_user(2, "stranger", role="user")
        seed_agent("agent-a", 1)
        seed_agent("agent-x", 2)
        parent_id = _seed_parent("agent-a")

        child_id = _run(_create_child(
            name="agent-x", current_user=_user(2, "stranger"),
            x_source_agent="agent-a",          # spoofed: == the parent's agent
            parent_id=parent_id))

        child = db.get_execution(child_id)
        assert child.source_channel is None, (
            "a human caller satisfied the AGENT arm with a forged header — the "
            "provenance guard must key on the principal, not the header"
        )
        assert child.source_channel_agent is None

    def test_shared_accessor_cannot_route_a_report_into_the_owners_chat(
            self, db_backend):
        """The human arm is OWNER-or-admin, not any accessor. Posting into a
        channel chat is a proactive-send capability and every other proactive
        surface is owner-gated (`OwnedAgentByName`) or per-recipient-consented
        (#321); a share recipient can already read the owner's execution ids
        (`GET /api/executions` is accessor-scoped), so an accessor arm would let
        them push a report into the owner's Telegram DM or group."""
        from sqlalchemy import text

        from database import db
        from db.engine import get_engine

        seed_user(1, "owner", role="user")
        seed_user(2, "colleague", role="user")
        seed_agent("agent-a", 1)
        with get_engine().begin() as conn:
            conn.execute(text("UPDATE users SET email = :e WHERE id = 2"),
                         {"e": "colleague@example.com"})
            conn.execute(text(
                "INSERT INTO agent_sharing "
                "(agent_name, shared_with_email, shared_by_id, created_at) "
                "VALUES ('agent-a', 'colleague@example.com', 1, :n)"),
                {"n": "2026-01-01T00:00:00Z"})
        parent_id = _seed_parent("agent-a")

        # Precondition: the colleague genuinely HAS access (so this pins the
        # accessor→owner narrowing, not a plain no-access refusal).
        assert db.can_user_access_agent("colleague", "agent-a") is True

        child_id = _run(_create_child(
            name="agent-a", current_user=_user(2, "colleague"),
            parent_id=parent_id))

        child = db.get_execution(child_id)
        assert child.source_channel is None
        assert child.source_channel_agent is None

    def test_connector_principal_gets_no_inheritance(self, db_backend):
        """A connector key is consumption-only (ent#46) but resolves to the
        OWNER, so the owner arm would otherwise make it owner-equivalent for
        this capability — the exact thing `_enforce_connector_scope` exists to
        prevent."""
        from database import db

        seed_user(1, "owner", role="admin")
        seed_agent("agent-a", 1)
        parent_id = _seed_parent("agent-a")

        child_id = _run(_create_child(
            name="agent-a",
            current_user=_user(role="admin", connector_agent="agent-a"),
            parent_id=parent_id))

        child = db.get_execution(child_id)
        assert child.source_channel is None

    def test_two_hop_inheritance_carries_root_binding_agent(self, db_backend):
        """Transitive A→B→C: C's row carries the ROOT binding agent A, not the
        middle hop — the bot the user actually addressed delivers."""
        from database import db

        seed_user(1, "owner", role="admin")
        for a in ("agent-a", "worker-b", "worker-c"):
            seed_agent(a, 1)
        root_id = _seed_parent("agent-a")

        mid_id = _run(_create_child(
            name="worker-b", current_user=_user(role="admin", agent_name="agent-a"),
            x_source_agent="agent-a", parent_id=root_id))
        leaf_id = _run(_create_child(
            name="worker-c", current_user=_user(role="admin", agent_name="worker-b"),
            x_source_agent="worker-b", parent_id=mid_id))

        leaf = db.get_execution(leaf_id)
        assert leaf.source_channel == "telegram"
        assert leaf.source_channel_chat_id == "-100777"
        assert leaf.source_channel_agent == "agent-a"


class TestRealEffectGuard:
    """D9/M1 pin with the REAL guard resolution. The row deliberately carries
    ``source_channel_agent != agent_name``: if the reporter ever passes
    binding_agent into effect_guard, resolve_and_validate_execution fail-opens
    (dedup silently DISABLED) and the replay test posts twice → red."""

    @pytest.fixture
    def telegram_world(self, db_backend, encryption_key, monkeypatch):
        from database import db

        db.create_telegram_binding("agent-a", "tok-secret", bot_username="abot",
                                   bot_id="111")
        binding = db.get_telegram_binding("agent-a")
        db.get_or_create_telegram_group_config(binding["id"], "-100777",
                                               chat_title="ops", chat_type="group")

        from adapters.telegram_adapter import TelegramAdapter

        sent = []

        async def _tg_send(self, bot_token, chat_id, text, reply_to_message_id=None,
                           parse_mode="HTML"):
            sent.append({"bot_token": bot_token, "chat_id": chat_id, "text": text})
            return {"message_id": 999}

        monkeypatch.setattr(TelegramAdapter, "_send_message", _tg_send)
        return sent

    def _seed_delegated_row(self, agent="worker-b"):
        from database import db

        row = db.create_task_execution(
            agent_name=agent, message="delegated", triggered_by="agent",
            source_channel="telegram", source_channel_chat_id="-100777",
            source_channel_thread="42", source_channel_agent="agent-a",
        )
        return row.id

    def test_telegram_replay_does_not_repost(self, telegram_world):
        sent = telegram_world
        eid = self._seed_delegated_row()

        assert _run(ccr.report_completion(
            execution_id=eid, agent_name="worker-b",
            status="success", summary_or_error="done")) is True
        assert len(sent) == 1
        assert sent[0]["bot_token"] == "tok-secret"   # A's bot delivered

        # Re-delivered terminal (the pull-mode / #1083 replay shape):
        assert _run(ccr.report_completion(
            execution_id=eid, agent_name="worker-b",
            status="success", summary_or_error="done")) is False
        assert len(sent) == 1, (
            "the completion was posted twice — effect_guard resolution "
            "fail-opened; is the guard being keyed on binding_agent? (D9/M1)"
        )

    def test_one_report_per_fanout_child(self, telegram_world):
        """G3/D8 pin: A delegating to B and C in parallel yields ONE report per
        child execution — per-execution identity, inherited Slack semantics."""
        sent = telegram_world
        e1 = self._seed_delegated_row("worker-b")
        e2 = self._seed_delegated_row("worker-c")

        for eid, agent in ((e1, "worker-b"), (e2, "worker-c")):
            assert _run(ccr.report_completion(
                execution_id=eid, agent_name=agent,
                status="success", summary_or_error="done")) is True
        assert len(sent) == 2


class TestLiveColumnSelects:
    """learnings 2026-07-19: schema-parity cannot catch a missed tables.py —
    a live SELECT through the Core metadata against a real DB can."""

    def test_source_channel_agent_is_selectable(self, db_backend):
        from sqlalchemy import select

        from db.engine import get_engine
        from db.tables import schedule_executions

        with get_engine().connect() as conn:
            rows = conn.execute(
                select(schedule_executions.c.source_channel_agent)).fetchall()
        assert rows == []

    def test_allow_proactive_is_selectable(self, db_backend):
        from sqlalchemy import select

        from db.engine import get_engine
        from db.tables import telegram_group_configs

        with get_engine().connect() as conn:
            rows = conn.execute(
                select(telegram_group_configs.c.allow_proactive)).fetchall()
        assert rows == []


class TestGroupConfigConsentRoundTrip:
    def test_new_group_defaults_to_allow(self, db_backend, encryption_key):
        """D2: uniform default ALLOW — a new-groups-deny split would make the
        flagship @mention-delegation silently dead in every NEW group."""
        from database import db

        db.create_telegram_binding("agent-a", "tok")
        binding = db.get_telegram_binding("agent-a")
        cfg = db.get_or_create_telegram_group_config(binding["id"], "-1", "g", "group")
        assert cfg["allow_proactive"] is True

    def test_toggle_round_trip_through_facade(self, db_backend, encryption_key):
        from database import db

        db.create_telegram_binding("agent-a", "tok")
        binding = db.get_telegram_binding("agent-a")
        cfg = db.get_or_create_telegram_group_config(binding["id"], "-1", "g", "group")

        updated = db.update_telegram_group_config(
            cfg["id"], allow_proactive=False)
        assert updated["allow_proactive"] is False
        # …and the other fields were not clobbered by the kwargs passthrough
        # (eng M3 — the positional-append swap this guards against).
        assert updated["trigger_mode"] == cfg["trigger_mode"]

        again = db.get_telegram_group_config(binding["id"], "-1")
        assert again["allow_proactive"] is False


class TestRouterPut:
    def test_agent_principal_cannot_flip_consent(self):
        """ent#265: the allow_proactive arm is human-only — an agent-scoped key
        resolves to the OWNER, so without the guard an agent could self-grant
        the very consent this feature adds (ent#223's post-ship pitfall)."""
        from fastapi import HTTPException

        from models import TelegramGroupConfigUpdateRequest
        from routers.telegram import update_telegram_group

        agent_key_user = _user(1, "owner", role="admin")
        agent_key_user.agent_name = "worker-b"          # scope='agent' marker

        with pytest.raises(HTTPException) as exc:
            _run(update_telegram_group(
                agent_name="agent-a", group_config_id=1,
                config=TelegramGroupConfigUpdateRequest(allow_proactive=True),
                current_user=agent_key_user,
            ))
        assert exc.value.status_code == 403

    def test_agent_principal_can_still_update_trigger_mode(
            self, db_backend, encryption_key):
        """The surgical gate: existing agent-callable trigger_mode/welcome
        updates keep working — only the consent arm is fenced."""
        from database import db
        from models import TelegramGroupConfigUpdateRequest
        from routers.telegram import update_telegram_group

        db.create_telegram_binding("agent-a", "tok")
        binding = db.get_telegram_binding("agent-a")
        cfg = db.get_or_create_telegram_group_config(binding["id"], "-1", "g", "group")

        agent_key_user = _user(1, "owner", role="admin")
        agent_key_user.agent_name = "agent-a"

        updated = _run(update_telegram_group(
            agent_name="agent-a", group_config_id=cfg["id"],
            config=TelegramGroupConfigUpdateRequest(trigger_mode="all"),
            current_user=agent_key_user,
        ))
        assert updated["trigger_mode"] == "all"
        assert updated["allow_proactive"] is True       # untouched

    def test_human_put_round_trip(self, db_backend, encryption_key):
        from database import db
        from models import TelegramGroupConfigUpdateRequest
        from routers.telegram import update_telegram_group

        db.create_telegram_binding("agent-a", "tok")
        binding = db.get_telegram_binding("agent-a")
        cfg = db.get_or_create_telegram_group_config(binding["id"], "-1", "g", "group")

        updated = _run(update_telegram_group(
            agent_name="agent-a", group_config_id=cfg["id"],
            config=TelegramGroupConfigUpdateRequest(allow_proactive=False),
            current_user=_user(1, "owner", role="admin"),
        ))
        assert updated["allow_proactive"] is False
        assert db.get_telegram_group_config(binding["id"], "-1")["allow_proactive"] is False
