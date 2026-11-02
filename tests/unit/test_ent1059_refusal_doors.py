"""
Every door answers an admission-policy refusal (trinity-enterprise#1059, review C2).

A turn an admission policy stops inside ``execute_task`` is a SKIPPED row
``[usage_share] <sentence>`` — not a failure, not a success. Before this file
every ``dispatch_and_await_terminal`` caller branched on ``failed``/``cancelled``
only, so a refused turn came back success-shaped with an empty reply: the paid
door settled a charge for a turn that never ran, the MCP inline door answered
200 "", a channel posted an empty message, the public link stored an empty
assistant turn, a room said "could not respond", and the Workspace lost the
refusal whenever the turn had been refused on another worker.

The fix is one rule in one place — ``chat_signals.admission_refusal_of`` reads
the in-memory refusal or rebuilds it from the row's ``skipped`` + code shape,
and ``result_from_execution_row`` attaches it — and one branch per door. Each
test here drives its door with the REAL result rebuilt from a REAL SKIPPED row
(``db_harness``), the way a turn refused on another worker arrives.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend, run as _hrun  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.chat_signals import AdmissionRefused, admission_refusal_of  # noqa: E402

# The Workspace classifier harness (the REAL `portal_chat`, boundaries stubbed).
from test_2320_portal_failed_turn_visibility import chat as portal  # noqa: E402,F401

pytestmark = pytest.mark.unit

AGENT = "finance"
STOP = ("finance has used 15 of its 15 usage budget for the last 5 hours, so every door "
        "stops until the share is raised or the window frees at 2026-11-02 14:00 UTC. "
        "An ask to raise the share has been filed to the agent's owner; nothing raises it automatically.")


@pytest.fixture
def refused_row(db_backend):
    """A real execution row closed the way `_admission_policy_gate` closes it."""
    from database import db
    row = db.create_task_execution(agent_name=AGENT, message="tick", triggered_by="schedule")
    _hrun("UPDATE schedule_executions SET status = 'skipped', error = :e WHERE id = :i",
          e=f"[usage_share] {STOP}", i=row.id)
    return row.id


@pytest.fixture
def refused_result(refused_row):
    """The result every door reads back from that row."""
    from services.task_execution_service import result_from_execution_row
    return result_from_execution_row(refused_row)


@pytest.fixture
def adapter(monkeypatch, refused_result):
    """`dispatch_and_await_terminal` answering the rebuilt result, patched on its
    own module where the doors' lazy imports look it up (the #3114 harness)."""
    import services.task_execution_service as tes
    mock = AsyncMock(return_value=refused_result)
    monkeypatch.setattr(tes, "dispatch_and_await_terminal", mock)
    return mock


# ---------------------------------------------------------------------------
# The rule, and the row → result rebuild
# ---------------------------------------------------------------------------

def test_the_row_comes_back_carrying_the_refusal(refused_result):
    """`result_from_execution_row` rebuilds the refusal: status, stripped error,
    the code, and the attribute the in-worker gate would have attached."""
    assert refused_result.status == "skipped"
    assert refused_result.error == STOP
    assert refused_result.error_code.value == "usage_share"
    refusal = refused_result.admission_refusal
    assert isinstance(refusal, AdmissionRefused)
    assert (refusal.code, refusal.agent_name, refusal.message) == ("usage_share", AGENT, STOP)
    assert refusal.frees_at is None   # not on the row; the sentence says when


def test_the_predicate_reads_the_attribute_or_the_rows_shape_and_nothing_else():
    from services.execution_envelope import TaskExecutionErrorCode
    attached = AdmissionRefused("usage_share", AGENT, STOP)
    assert admission_refusal_of(SimpleNamespace(admission_refusal=attached), AGENT) is attached
    shaped = SimpleNamespace(status="skipped", error_code=TaskExecutionErrorCode.USAGE_SHARE,
                             error=STOP, admission_refusal=None)
    rebuilt = admission_refusal_of(shaped, AGENT)
    assert (rebuilt.code, rebuilt.message) == ("usage_share", STOP)
    # Other SKIPPEDs (a gate hold, "no schedule fired") and other codes are not refusals.
    assert admission_refusal_of(SimpleNamespace(status="skipped", error_code=None, error="held"), AGENT) is None
    assert admission_refusal_of(SimpleNamespace(status="failed", error_code=TaskExecutionErrorCode.USAGE_SHARE,
                                                error=STOP), AGENT) is None
    assert admission_refusal_of(SimpleNamespace(status="success", error_code=None, error=None), AGENT) is None


# ---------------------------------------------------------------------------
# 1. the paid door — no attach, no settle, no charge
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_paid_door_charges_nothing_for_a_refused_turn(refused_result):
    """The money bug the review named: before, a SKIPPED refusal was attached
    to the claim, logged verify-ok and SETTLED with an empty response."""
    from test_ent679_paid_turn_service import _drive
    from services import paid_turn_service as pts

    async def _execute():
        return refused_result

    outcome, ctx = await _drive(execute=_execute)

    assert outcome.kind == pts.EXECUTION_FAILED
    assert outcome.payload["status"] == "skipped"
    assert outcome.payload["error_code"] == "usage_share"
    assert outcome.payload["payment"] == {"settled": False, "reason": STOP}
    assert ctx.payments.settle_calls == []          # never settled
    assert ctx.idem.attached == []                   # never attached to the claim
    assert ctx.idem.completed == [] and len(ctx.idem.failed) == 1   # claim released
    assert ctx.db.actions() == ["verify"]            # no settle / settle_failed row


# ---------------------------------------------------------------------------
# 2. the MCP inline-auth chat — 429 with the sentence and the code header
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_mcp_inline_door_answers_429_with_the_code(monkeypatch, adapter):
    from fastapi import HTTPException
    from services import mcp_auth_service as mas

    monkeypatch.setattr(mas, "assert_email_may_reach_agent", lambda email, agent: None)
    monkeypatch.setattr(mas.db, "get_public_channel_model", lambda agent: None)

    with pytest.raises(HTTPException) as info:
        await mas.dispatch_chat("ada@example.com", AGENT, "hello")

    assert info.value.status_code == 429
    assert info.value.detail == STOP
    assert info.value.headers["X-Trinity-Error-Code"] == "usage_share"
    assert "Retry-After" not in info.value.headers   # the row carries no frees_at


# ---------------------------------------------------------------------------
# 3. a channel — the sentence is sent; no empty assistant turn is persisted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_channel_sends_the_sentence_and_persists_no_empty_turn(monkeypatch, adapter):
    """The `_run_agent_task` harness of `test_ent751_gate_callers`."""
    import adapters.message_router as mr

    monkeypatch.setattr(mr, "get_task_execution_service", lambda: MagicMock())
    monkeypatch.setattr(mr, "build_public_channel_caller_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(mr, "build_voice_capability_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(mr, "_get_channel_allowed_tools", lambda: ["WebSearch"])
    monkeypatch.setattr(mr.db, "get_public_channel_model", lambda agent: None)
    channel = MagicMock()
    channel.get_source_identifier.return_value = "telegram:9001:424242"
    channel.send_response = AsyncMock()
    channel.indicate_done = AsyncMock()
    message = SimpleNamespace(text="hello", channel_id="424242", thread_id=None, files=[], metadata={})
    router = mr.ChannelMessageRouter.__new__(mr.ChannelMessageRouter)

    out = await router._run_agent_task(
        channel, message, AGENT, "bot-token", "telegram", False,
        container=None, upload_dir=None, context_prompt="Sender: ada\n\nhello",
        verified_email=None, image_data=[])

    # None, as for a failed turn: step 12 never runs, so no assistant turn with
    # an empty body is written to the channel session.
    assert out is None
    channel.send_response.assert_awaited_once()
    assert channel.send_response.await_args.args[1].text == STOP


# ---------------------------------------------------------------------------
# 4. the public link — sync 429; async stores the sentence, never ""
# ---------------------------------------------------------------------------

def _public(monkeypatch):
    """The `_public` harness of `test_3114_pull_route_callers`."""
    from services import public_chat_service as pcs

    db = MagicMock()
    db.get_access_policy.return_value = {}
    db.count_recent_messages_by_ip.return_value = 0
    db.count_recent_messages_by_token.return_value = 0
    db.get_or_create_public_chat_session.return_value = SimpleNamespace(id="cs-7")
    db.build_public_chat_context.return_value = "Previous conversation: …\n\nCurrent message: …"
    db.get_public_chat_session.return_value = SimpleNamespace(message_count=2)
    db.get_public_channel_model.return_value = None
    db.create_task_execution.return_value = SimpleNamespace(id="e-async")
    monkeypatch.setattr(pcs, "db", db)
    monkeypatch.setattr(pcs, "get_agent_container", lambda n: SimpleNamespace(status="running"))
    monkeypatch.setattr(pcs, "build_public_channel_caller_prompt", lambda *a, **k: None)
    return pcs, db


def _assistant_turns(db):
    return [c.kwargs for c in db.add_public_chat_message.call_args_list
            if c.kwargs.get("role") == "assistant"]


@pytest.mark.asyncio
async def test_the_public_link_answers_429_and_stores_no_empty_turn(monkeypatch, adapter):
    pcs, db = _public(monkeypatch)
    request = SimpleNamespace(session_token=None, session_id="anon-1", files=None,
                              message="hello", async_mode=False)

    with pytest.raises(pcs.PublicChatError) as info:
        await pcs.run_public_chat({"id": "link-1", "agent_name": AGENT}, request, "1.2.3.4")

    assert info.value.status_code == 429
    assert info.value.detail == STOP
    assert info.value.headers["X-Trinity-Error-Code"] == "usage_share"
    assert _assistant_turns(db) == []


@pytest.mark.asyncio
async def test_the_public_background_turn_stores_the_sentence_as_the_reply(monkeypatch, adapter):
    pcs, db = _public(monkeypatch)

    await pcs._execute_public_chat_background(
        agent_name=AGENT, context_prompt="ctx", source_email="v@example.com",
        execution_id="e1", chat_session_id="cs-9", session_identifier="v@example.com",
        identifier_type="email", verified_email="v@example.com", request_text="hello")

    assert _assistant_turns(db) == [dict(session_id="cs-9", role="assistant", content=STOP,
                                         sender_email="v@example.com")]


# ---------------------------------------------------------------------------
# 5. the Workspace — the row's shape alone (a drained turn) is enough
# ---------------------------------------------------------------------------

def test_the_workspace_classifies_a_drained_refusal(portal, refused_result):
    """`portal_chat` used to read only the in-memory attribute; a turn refused on
    another worker carried none and fell through to a success-shaped empty
    reply. The classifier answers `limit` from status + code alone."""
    from test_2320_portal_failed_turn_visibility import _refuse
    svc, state = portal
    refused_result.admission_refusal = None   # as a drained result arrived before the fix
    err = _refuse(svc, state, result=refused_result)
    assert err.status_code == 429
    assert err.category == "limit"
    assert err.retryable is False
    assert err.detail == STOP


# ---------------------------------------------------------------------------
# 6. a room — the policy's sentence, not "could not respond"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_room_posts_the_policys_sentence(monkeypatch, adapter):
    """The room-wake harness of `test_3114_pull_route_callers`."""
    from shared_sessions import service

    room = "room-1"
    monkeypatch.delenv("PULL_MODE_PILOT_AGENTS", raising=False)
    delta = [{"seq": 4, "sender_kind": "user", "sender_identity": "c@example.com",
              "content": f"@{AGENT} hello", "kind": "message"}]
    monkeypatch.setattr(service.db, "get_participant",
                        lambda *a, **k: {"last_read_seq": 3, "cached_session_id": "s1"})
    monkeypatch.setattr(service.db, "get_room", lambda *a, **k: {
        "id": room, "name": "Room", "status": "open", "topic": None})
    monkeypatch.setattr(service.db, "get_messages", lambda *a, **k: delta)
    monkeypatch.setattr(service.db, "list_participants", lambda _r: [
        {"kind": "agent", "identity": AGENT, "left_at": None}])
    advanced, cleared = [], []
    monkeypatch.setattr(service.db, "advance_read_cursor", lambda *a, **k: advanced.append(a))
    monkeypatch.setattr(service.db, "clear_cached_session", lambda *a, **k: cleared.append(a))
    for name in ("_mark_agent_working", "_clear_agent_working", "_broadcast"):
        monkeypatch.setattr(service, name, lambda *a, **k: None)
    monkeypatch.setattr(service, "_room_inbox_context", AsyncMock(return_value=("", [])))
    posted: list = []
    monkeypatch.setattr(service, "_post_system",
                        lambda room_id, content: posted.append((room_id, content)))
    post_message = AsyncMock(return_value={})
    monkeypatch.setattr(service, "post_message", post_message)

    await service._wake_agent(SimpleNamespace(email="c@example.com"), room, AGENT, 0)

    assert posted == [(room, STOP)]
    post_message.assert_not_called()
    assert cleared == []     # the resume handle is not dead
    assert advanced == []    # the delta is re-delivered once the share is raised


# ---------------------------------------------------------------------------
# 7. A2A and validation — named, and consistent with the row reader
# ---------------------------------------------------------------------------

def test_a2a_reports_a_refused_turn_rejected_like_the_row_reader():
    from routers import a2a
    assert a2a._a2a_state_for("skipped") == a2a._ROW_STATE["skipped"] == "rejected"


def test_a_refused_validation_turn_validated_nothing(refused_result):
    from services.validation_service import ValidationService, ValidationStatus
    svc = ValidationService.__new__(ValidationService)
    out = svc._parse_validation_response(refused_result)
    assert out.status == ValidationStatus.ERROR
    assert STOP in out.summary
