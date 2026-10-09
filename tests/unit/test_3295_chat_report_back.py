"""A sequential ``chat_with_agent`` call reports back to the caller's conversation (#3295).

The MCP server arms the report at the one moment it knows the caller did NOT
get the reply — when it hands back a ``queued_timeout`` receipt — through
``POST /api/agents/{name}/executions/{id}/report-back``. The backend stamps the
caller's turn's channel context onto the ``/chat`` row (ent#498's
``stamp_execution_channel_context``, behind the ent#265 provenance guard) and
the ``/chat`` terminals spawn the same consent-gated completion report a
``/task`` child gets. A row that answered inline is never stamped, so it can
never post a second "done".

What is pinned here, against the real db layer (db_harness; SQLite, and
Postgres when TEST_POSTGRES_URL is set):

* the arm stamps the row and the row is READ BACK (agent arm and human arm);
* every refusal: not the row's dispatcher, a connector key, a foreign or
  missing row (uniform 404), an inbound channel turn, a parent that is not
  running, a parent the caller does not own; a retried arm is idempotent;
* an arm that lands AFTER the terminal spawns the report itself, and an arm
  that lands BEFORE it leaves the report to the terminal writer — each with the
  REAL ``report_completion`` + ``effect_guard``, so the two orders deliver
  exactly once and a second terminal spawn is a replay, not a repost;
* ``report_completion`` reads the row fresh (the property that makes the
  arm-vs-terminal race safe without a RETURNING write);
* an un-armed ``/chat`` row reports nothing at its terminal;
* the push ``/chat`` success finalizer spawns on a CAS-won write only.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
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

from db.write_params import ExecutionResult, TaskExecutionFields  # noqa: E402
from models import ReportBackRequest, TaskExecutionStatus  # noqa: E402
from services import channel_completion_report as ccr  # noqa: E402
from services import chat_execution_service as ces  # noqa: E402
from services.chat_signals import ChatDispatchError  # noqa: E402

pytestmark = pytest.mark.unit

CALLER = "agent-a"      # serves the Telegram group; delegates sequentially
WORKER = "worker-b"     # the sequential child


def _run(coro):
    return asyncio.run(coro)


def _user(user_id=1, username="owner", role="user", agent_name=None,
          connector_agent=None):
    from models import User

    return User(id=user_id, username=username, role=role,
                email=f"{username}@example.com", agent_name=agent_name,
                connector_agent=connector_agent)


def _seed_parent(agent=CALLER, *, channel="telegram", chat_id="-100777", thread="42"):
    """The caller's own turn: an inbound channel turn, still running."""
    from database import db

    row = db.create_task_execution(
        agent_name=agent, message="parent turn", triggered_by=channel,
        fields=TaskExecutionFields(
            source_channel=channel, source_channel_chat_id=chat_id,
            source_channel_thread=thread,
        ),
    )
    return row.id


def _seed_chat_child(*, agent=WORKER, triggered_by="agent", source_agent=CALLER,
                     source_user_id=1, message="delegated"):
    """What ``prepare_chat_execution`` writes for a sequential /chat call: no
    channel context, the dispatcher on ``source_agent_name`` / ``source_user_id``."""
    from database import db

    row = db.create_task_execution(
        agent_name=agent, message=message, triggered_by=triggered_by,
        fields=TaskExecutionFields(
            source_user_id=source_user_id, source_user_email="owner@example.com",
            source_agent_name=source_agent,
        ),
    )
    return row.id


def _finish(execution_id, status=TaskExecutionStatus.SUCCESS, *, response="all done",
            error=None):
    from database import db

    assert db.update_execution_status(
        execution_id=execution_id, status=status,
        result=ExecutionResult(response=response, error=error),
    )


async def _arm(child_id, parent_id, user, *, name=WORKER):
    return await ces.arm_chat_report_back(
        name=name, execution_id=child_id,
        request=ReportBackRequest(parent_execution_id=parent_id), current_user=user,
    )


@pytest.fixture
def world(db_backend):
    seed_user(1, "owner", role="user")
    seed_user(2, "stranger", role="user")
    seed_agent(CALLER, 1)
    seed_agent(WORKER, 1)
    seed_agent("agent-evil", 2)
    return db_backend


# ---------------------------------------------------------------------------
# The arm stamps the row — read back, both provenance arms
# ---------------------------------------------------------------------------

class TestArmStampsTheRow:
    def test_agent_arm_inherits_the_callers_context(self, world):
        """A (agent key) delegated to B over /chat and got a receipt; arming B's
        row with A's turn copies the Telegram destination onto it, binding agent
        = A (the bot the person addressed delivers)."""
        from database import db

        parent = _seed_parent()
        child = _seed_chat_child()
        out = _run(_arm(child, parent, _user(agent_name=CALLER)))
        assert out["armed"] is True
        assert out["status"] == "running"
        row = db.get_execution(child)
        assert row.source_channel == "telegram"
        assert row.source_channel_chat_id == "-100777"
        assert row.source_channel_thread == "42"
        assert row.source_channel_agent == CALLER

    def test_human_arm_is_the_owner_who_dispatched(self, world):
        """A person's MCP key dispatched the /chat (triggered_by=mcp, no source
        agent); they own the parent's agent, so they inherit."""
        from database import db

        parent = _seed_parent()
        child = _seed_chat_child(triggered_by="mcp", source_agent=None, source_user_id=1)
        out = _run(_arm(child, parent, _user(1, "owner")))
        assert out["armed"] is True
        assert db.get_execution(child).source_channel == "telegram"

    def test_a_retried_arm_is_idempotent(self, world):
        parent = _seed_parent()
        child = _seed_chat_child()
        user = _user(agent_name=CALLER)
        assert _run(_arm(child, parent, user))["armed"] is True
        assert _run(_arm(child, parent, user))["armed"] is True

    def test_the_request_model_is_strict(self):
        with pytest.raises(Exception):
            ReportBackRequest(parent_execution_id="")
        with pytest.raises(Exception):
            ReportBackRequest(parent_execution_id="x", extra="y")
        with pytest.raises(Exception):
            ReportBackRequest(parent_execution_id="x" * 129)


    def test_a_row_the_real_chat_setup_wrote_can_be_armed(self, world):
        """AC5 on the backend side: the row ``prepare_chat_execution`` writes for
        an agent-to-agent /chat (dispatcher on ``source_agent_name``, owner on
        ``source_user_id``, trigger ``agent``) is exactly what the arm admits —
        not a hand-seeded look-alike."""
        from database import db
        from services.idempotency_service import IdempotencyDecision

        parent = _seed_parent()
        with (
            patch.object(ces, "activity_service",
                         MagicMock(track_activity=AsyncMock(return_value="act-x"))),
            patch.object(ces, "broadcast_collaboration_event", AsyncMock()),
        ):
            ctx = _run(ces.prepare_chat_execution(
                name=WORKER,
                request=SimpleNamespace(message="hello", model=None),
                current_user=_user(agent_name=CALLER),
                x_source_agent=CALLER, x_via_mcp="true",
                idem=IdempotencyDecision(enabled=False, replay=False, in_flight=False),
                chat_execution_id="q-1",
                capacity_result=SimpleNamespace(state="immediate"),
                queue_result="immediate",
            ))
        child = ctx.task_execution_id
        row = db.get_execution(child)
        assert row.triggered_by == "agent"
        assert row.source_channel is None, "the /chat row is created without a destination"

        out = _run(_arm(child, parent, _user(agent_name=CALLER)))
        assert out["armed"] is True
        row = db.get_execution(child)
        assert (row.source_channel, row.source_channel_chat_id, row.source_channel_agent) == (
            "telegram", "-100777", CALLER)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

class TestRefusals:
    def test_only_the_dispatcher_may_arm(self, world):
        """An agent that merely has access to the worker cannot attach a
        destination to a row it did not dispatch — and learns nothing: the
        refusal is the same uniform 404 a missing row gets (Invariant #8)."""
        from database import db

        parent = _seed_parent()
        child = _seed_chat_child()                       # dispatched by agent-a
        with pytest.raises(ChatDispatchError) as ei:
            _run(_arm(child, parent, _user(2, "stranger", agent_name="agent-evil")))
        assert (ei.value.status_code, ei.value.detail) == (404, "Execution not found")
        assert db.get_execution(child).source_channel is None

    def test_a_person_who_did_not_dispatch_is_refused(self, world):
        parent = _seed_parent()
        child = _seed_chat_child(triggered_by="mcp", source_agent=None, source_user_id=1)
        with pytest.raises(ChatDispatchError) as ei:
            _run(_arm(child, parent, _user(2, "stranger")))
        assert (ei.value.status_code, ei.value.detail) == (404, "Execution not found")

    def test_connector_keys_are_refused(self, world):
        parent = _seed_parent()
        child = _seed_chat_child(triggered_by="mcp", source_agent=None, source_user_id=1)
        with pytest.raises(ChatDispatchError) as ei:
            _run(_arm(child, parent, _user(1, "owner", connector_agent=CALLER)))
        assert ei.value.status_code == 403

    def test_a_foreign_or_missing_row_is_a_uniform_404(self, world):
        parent = _seed_parent()
        child = _seed_chat_child()
        for execution_id, name in ((child, CALLER), ("no-such-row", WORKER)):
            with pytest.raises(ChatDispatchError) as ei:
                _run(_arm(execution_id, parent, _user(agent_name=CALLER), name=name))
            assert ei.value.status_code == 404
            assert ei.value.detail == "Execution not found"

    def test_an_inbound_channel_turn_is_never_armed(self, world):
        """The adapter answers it inline; arming it would post twice."""
        from database import db

        parent = _seed_parent()
        child = _seed_chat_child(triggered_by="slack")
        out = _run(_arm(child, parent, _user(agent_name=CALLER)))
        assert out == {"armed": False, "execution_id": child, "reason": "not_a_chat_turn"}
        assert db.get_execution(child).source_channel is None

    def test_a_finished_parent_yields_no_context(self, world):
        """ent#457: inheritance is for work delegated DURING a live turn."""
        from database import db

        parent = _seed_parent()
        _finish(parent)
        child = _seed_chat_child()
        out = _run(_arm(child, parent, _user(agent_name=CALLER)))
        assert out["armed"] is False
        assert out["reason"] == "no_inherited_context"
        assert db.get_execution(child).source_channel is None

    def test_the_provenance_guard_still_decides_the_parent(self, world):
        """ent#265: the agent arm must BE the parent's executing agent. agent-evil
        dispatched this /chat itself (so it IS the dispatcher) but names
        agent-a's turn as the parent — refused, no destination."""
        from database import db

        parent = _seed_parent(CALLER)
        child = _seed_chat_child(source_agent="agent-evil", source_user_id=2)
        out = _run(_arm(child, parent, _user(2, "stranger", agent_name="agent-evil")))
        assert out["armed"] is False
        assert out["reason"] == "no_inherited_context"
        assert db.get_execution(child).source_channel is None


# ---------------------------------------------------------------------------
# Delivery — the real reporter, both orders of arm vs terminal
# ---------------------------------------------------------------------------

@pytest.fixture
def telegram_world(world, monkeypatch):
    """agent-a's Telegram binding + a consented group; the send is captured."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    from database import db

    db.create_telegram_binding(CALLER, "tok-secret", bot_username="abot", bot_id="111")
    binding = db.get_telegram_binding(CALLER)
    db.get_or_create_telegram_group_config(binding["id"], "-100777",
                                           chat_title="ops", chat_type="group")

    from adapters.telegram_adapter import TelegramAdapter

    sent = []

    async def _tg_send(self, bot_token, chat_id, text, reply_to_message_id=None,
                       parse_mode="HTML"):
        sent.append({"bot_token": bot_token, "chat_id": chat_id, "text": text,
                     "reply_to": reply_to_message_id})
        return {"message_id": 999}

    monkeypatch.setattr(TelegramAdapter, "_send_message", _tg_send)
    return sent


async def _drain_reports():
    """Await every fire-and-forget report ``spawn_completion_report`` started."""
    while ccr._inflight:
        await asyncio.gather(*list(ccr._inflight), return_exceptions=True)


class TestDelivery:
    def test_arm_after_terminal_delivers_once(self, telegram_world):
        """The terminal landed first (nothing to report then); the arm re-reads
        the row, sees it finished, and spawns the report itself. The terminal
        writer's own spawn, replayed afterwards, is a dedup — not a second post."""
        sent = telegram_world
        parent = _seed_parent()
        child = _seed_chat_child()
        _finish(child, response="the answer")

        async def go():
            out = await _arm(child, parent, _user(agent_name=CALLER))
            await _drain_reports()
            return out

        out = _run(go())
        assert out["armed"] is True
        assert out["status"] == "success"
        assert len(sent) == 1
        assert sent[0]["bot_token"] == "tok-secret"        # A's bot delivered
        assert sent[0]["chat_id"] == "-100777"
        assert sent[0]["reply_to"] == "42"
        assert "the answer" in sent[0]["text"]

        # The finalizer's spawn arriving late is the replay the guard exists for.
        assert _run(ccr.report_completion(
            execution_id=child, agent_name=WORKER, status="success",
            summary_or_error="the answer")) is False
        assert len(sent) == 1

    def test_arm_before_terminal_leaves_the_report_to_the_terminal(self, telegram_world):
        """The usual order: the receipt (and the arm) come at ~25 s, the turn
        finishes minutes later. The arm spawns nothing on a running row; the
        terminal writer's spawn finds the stamped row and delivers once."""
        sent = telegram_world
        parent = _seed_parent()
        child = _seed_chat_child()

        async def go():
            out = await _arm(child, parent, _user(agent_name=CALLER))
            await _drain_reports()
            assert sent == [], "a running row has nothing to report yet"
            _finish(child, response="done later")
            # What _finalize_chat_success does on its CAS-won write:
            ces._spawn_chat_terminal_report(True, WORKER, child, TaskExecutionStatus.SUCCESS, "done later")
            await _drain_reports()
            return out

        out = _run(go())
        assert out["status"] == "running"
        assert len(sent) == 1
        assert "done later" in sent[0]["text"]

    def test_a_failure_terminal_reports_too(self, telegram_world):
        sent = telegram_world
        parent = _seed_parent()
        child = _seed_chat_child()

        async def go():
            await _arm(child, parent, _user(agent_name=CALLER))
            _finish(child, TaskExecutionStatus.FAILED, response=None, error="agent crashed")
            ces._spawn_chat_terminal_report(True, WORKER, child, TaskExecutionStatus.FAILED, "agent crashed")
            await _drain_reports()

        _run(go())
        assert len(sent) == 1
        assert "agent crashed" in sent[0]["text"]

    def test_report_completion_reads_the_row_fresh(self, telegram_world):
        """The property that makes arm-vs-terminal safe without a RETURNING
        write: the reporter never trusts a caller's view of the row. A spawn
        issued while the row was unstamped still delivers if the stamp landed
        before the task ran."""
        sent = telegram_world
        parent = _seed_parent()
        child = _seed_chat_child()
        _finish(child, response="raced")

        async def go():
            # Spawned on the unstamped row (the terminal writer's view) …
            ccr.spawn_completion_report(execution_id=child, agent_name=WORKER,
                                        status="success", summary_or_error="raced")
            # … and the stamp lands before the task gets to run.
            await _arm(child, parent, _user(agent_name=CALLER))
            await _drain_reports()

        _run(go())
        assert len(sent) == 1, "two spawns, one destination, one post"

    def test_an_unarmed_chat_row_reports_nothing(self, telegram_world):
        """The no-double-post rule for this route: a sequential call that
        answered inline is never stamped, so its terminal has nowhere to go."""
        sent = telegram_world
        child = _seed_chat_child()
        _finish(child)
        assert _run(ccr.report_completion(
            execution_id=child, agent_name=WORKER, status="success",
            summary_or_error="all done")) is False
        assert sent == []


# ---------------------------------------------------------------------------
# The push /chat terminals spawn — CAS-won only
# ---------------------------------------------------------------------------

class TestPushTerminalSpawn:
    def test_helper_spawns_only_on_a_cas_win(self):
        with patch.object(ces.channel_completion_report, "spawn_completion_report") as spawn:
            ces._spawn_chat_terminal_report(False, WORKER, "e1", TaskExecutionStatus.SUCCESS, "x")
            ces._spawn_chat_terminal_report(True, WORKER, None, TaskExecutionStatus.SUCCESS, "x")
            spawn.assert_not_called()
            ces._spawn_chat_terminal_report(True, WORKER, "e1", TaskExecutionStatus.FAILED, "boom")
            spawn.assert_called_once_with(
                execution_id="e1", agent_name=WORKER, status="failed", summary_or_error="boom",
            )

    def test_success_finalizer_spawns_with_the_sanitized_response(self, world):
        """End to end through ``_finalize_chat_success`` against the real row:
        the CAS-won SUCCESS write is followed by one spawn carrying the
        sanitized response (what the row stores — never the raw body)."""
        from database import db

        child = _seed_chat_child()
        session = db.get_or_create_chat_session(
            agent_name=WORKER, user_id=1, user_email="owner@example.com")
        response = MagicMock()
        response.json.return_value = {
            "response": "token=sk-ant-SECRETSECRETSECRETSECRET done",
            "metadata": {"cost_usd": 0.01}, "session": {}, "execution_log": [],
        }
        activity = MagicMock(complete_activity=AsyncMock())
        with (
            patch.object(ces, "activity_service", activity),
            patch.object(ces.idempotency_service, "complete"),
            patch.object(ces.channel_completion_report, "spawn_completion_report") as spawn,
        ):
            _run(ces._finalize_chat_success(
                name=WORKER, response=response, start_time=__import__("datetime").datetime.utcnow(),
                session=session, current_user=_user(1, "owner"), chat_activity_id="a1",
                collaboration_activity_id=None, task_execution_id=child,
                _chat_subscription_id=None, execution=SimpleNamespace(id=child),
                queue_result="immediate", is_queued=False, idem=object(),
            ))
        spawn.assert_called_once()
        kwargs = spawn.call_args.kwargs
        assert kwargs["execution_id"] == child
        assert kwargs["status"] == "success"
        assert "sk-ant-SECRET" not in kwargs["summary_or_error"]
        assert "done" in kwargs["summary_or_error"]
        assert db.get_execution(child).status == TaskExecutionStatus.SUCCESS

        # A late FAILED over the SUCCESS row loses the CAS (a terminal is never
        # overwritten by a failure) → no second spawn.
        with patch.object(ces.channel_completion_report, "spawn_completion_report") as spawn2:
            lost = db.update_execution_status(
                execution_id=child, status=TaskExecutionStatus.FAILED,
                result=ExecutionResult(error="late"))
            assert not lost
            ces._spawn_chat_terminal_report(lost, WORKER, child, TaskExecutionStatus.FAILED, "late")
            spawn2.assert_not_called()
