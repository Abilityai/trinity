"""#4156 — the backend marks every turn that serves someone other than the owner
`disable_auto_memory`, on each wire to the agent.

The agent half (`test_4156_agent_auto_memory_spawn.py`) proves the flag turns
into `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` on the `claude` spawn. This half
proves which turns get the flag, and that every dispatch route carries it:

* the rule (`memory_subject.keeps_shared_auto_memory`) — read off the execution
  row by the same resolver the `write_user_memory` door uses, so "who this turn
  serves" has one answer. Auto-memory stays on only for the agent's own scope:
  a headless run, or a turn by the owner. A non-owner person, an anonymous
  public visitor, a channel group turn and any trigger off the allowlist get
  it OFF — every one of them is someone the shared directory must not hold;
* the push `/api/task` payload (`TaskExecutionService.execute_task`), which
  every producer reaches — public link, Workspace, channels, a drained backlog
  row, an approved gate run;
* the `/api/chat` payload (`chat_execution_service.build_chat_payload`);
* the pull claim envelope (`pull_coordination_service._build_claim_response`).

Fail-closed: a row that cannot be read turns auto-memory off for that turn.
The cost of a wrong "off" is one turn without the agent's auto-memory notes;
the cost of a wrong "on" is the leak this issue is about.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import memory_subject  # noqa: E402

from unit.test_2328_per_task_max_turns_wire import AGENT, EXEC, _wire  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "owner@example.com"
PERSON = "person@example.com"
OWNER_ID = 1
OTHER_ID = 2


def _row(**kw):
    base = dict(
        id=EXEC, agent_name=AGENT, status="running", triggered_by="public",
        source_user_email=None, schedule_id=None, source_channel=None,
        source_channel_client=None, source_agent_name=None, source_user_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row, keeps", [
    # The issue's turn: a signed-in person who is not the owner, in the Workspace.
    pytest.param(_row(triggered_by="public", source_user_email=PERSON), False, id="workspace-non-owner"),
    pytest.param(_row(triggered_by="public", source_user_email="anonymous (10.0.0.1)"), False, id="anonymous-public"),
    pytest.param(_row(triggered_by="chat", source_user_email=PERSON), False, id="chat-shared-user"),
    pytest.param(_row(triggered_by="slack", source_channel_client=PERSON), False, id="channel-one-to-one"),
    pytest.param(_row(triggered_by="telegram", source_user_email=OWNER), False, id="channel-group"),
    pytest.param(_row(triggered_by="schedule", schedule_id="s1", source_channel="portal",
                      source_channel_client=PERSON), False, id="seat-run"),
    pytest.param(_row(triggered_by="voice"), False, id="never-trigger"),
    pytest.param(_row(triggered_by="something_new"), False, id="unknown-trigger"),
    # The agent's own scope keeps today's behaviour.
    pytest.param(_row(triggered_by="public", source_user_email=OWNER), True, id="owner-in-workspace"),
    pytest.param(_row(triggered_by="chat", source_user_email=OWNER.upper()), True, id="owner-chat-case"),
    # Review of #4172: a non-owner admin is someone else (it was asserted True
    # with a different owner and labelled "owner"); the owner with no email is
    # the owner, by user id.
    pytest.param(_row(triggered_by="chat", source_user_email="admin", source_user_id=OTHER_ID), False,
                 id="non-owner-admin"),
    pytest.param(_row(triggered_by="chat", source_user_email="admin", source_user_id=OWNER_ID), True,
                 id="owner-admin-no-email-by-id"),
    # A shared user's `/task` (manual, no schedule) and MCP turn: their words.
    pytest.param(_row(triggered_by="manual", schedule_id="__manual__", source_user_email=PERSON,
                      source_user_id=OTHER_ID), False, id="task-shared-user"),
    pytest.param(_row(triggered_by="mcp", source_user_email=PERSON, source_user_id=OTHER_ID), False,
                 id="mcp-shared-user"),
    pytest.param(_row(triggered_by="manual", source_user_email=OWNER, source_user_id=OWNER_ID), True,
                 id="task-owner"),
    # A schedule's own fire is the schedule's run, whoever pressed Run now.
    pytest.param(_row(triggered_by="manual", schedule_id="s1", source_user_email=PERSON,
                      source_user_id=OTHER_ID), True, id="run-now-by-shared-user"),
    pytest.param(_row(triggered_by="retry", schedule_id="s1", source_user_id=OTHER_ID), True,
                 id="schedule-retry"),
    # Owner and headless turns the first rule turned off.
    pytest.param(_row(triggered_by="session", source_user_email=OWNER, source_user_id=OWNER_ID), True,
                 id="owner-session"),
    pytest.param(_row(triggered_by="session", source_user_email=PERSON, source_user_id=OTHER_ID), False,
                 id="shared-user-session"),
    pytest.param(_row(triggered_by="a2a", source_user_email=OWNER, source_user_id=OWNER_ID), True,
                 id="owner-a2a"),
    pytest.param(_row(triggered_by="a2a"), False, id="a2a-peer-with-no-user"),
    pytest.param(_row(triggered_by="operator_response"), True, id="operator-response"),
    pytest.param(_row(triggered_by="operator_response", source_channel="portal",
                      source_channel_client=PERSON), False, id="operator-response-to-a-seat"),
    pytest.param(_row(triggered_by="voip"), False, id="voip"),
    pytest.param(_row(triggered_by="schedule"), True, id="headless-schedule"),
    pytest.param(_row(triggered_by="agent", source_agent_name="caller"), True, id="agent-to-agent"),
    pytest.param(_row(triggered_by="schedule", schedule_id="s1", source_channel="portal",
                      source_channel_client=OWNER), True, id="seat-run-for-owner"),
])
def test_only_the_agents_own_scope_keeps_auto_memory(row, keeps):
    assert memory_subject.keeps_shared_auto_memory(row, OWNER, OWNER_ID) is keeps


def test_an_owner_with_no_email_keeps_it_by_id_and_nobody_else_does():
    admin_turn = _row(triggered_by="chat", source_user_email="admin", source_user_id=OWNER_ID)
    assert memory_subject.keeps_shared_auto_memory(admin_turn, None, OWNER_ID) is True
    other = _row(triggered_by="chat", source_user_email="admin", source_user_id=OTHER_ID)
    assert memory_subject.keeps_shared_auto_memory(other, None, OWNER_ID) is False


def test_a_person_turn_on_an_agent_with_no_resolvable_owner_is_off():
    assert memory_subject.keeps_shared_auto_memory(_row(source_user_email=PERSON), None) is False


def test_the_rule_reads_a_pull_claim_dict_row_too():
    row = vars(_row(source_user_email=PERSON))
    assert memory_subject.keeps_shared_auto_memory(row, OWNER) is False
    assert memory_subject.keeps_shared_auto_memory(vars(_row(triggered_by="schedule")), OWNER) is True


def test_unreadable_row_fails_closed(monkeypatch):
    from services import task_execution_service as tes

    assert tes.auto_memory_off_for_row(AGENT, None) is True
    monkeypatch.setattr(memory_subject, "keeps_shared_auto_memory",
                        MagicMock(side_effect=RuntimeError("boom")))
    assert tes.auto_memory_off_for_row(AGENT, _row(triggered_by="schedule")) is True


# ---------------------------------------------------------------------------
# The push /api/task wire
# ---------------------------------------------------------------------------

@pytest.fixture
def chat_layer(monkeypatch):
    import importlib

    from unittest.mock import AsyncMock

    ce = importlib.import_module("services.chat_execution_service")
    monkeypatch.setattr(ce, "run_post_turn_delivery", AsyncMock(return_value=None))
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")
    return ce


def _push(ce, row):
    with _wire(ce) as payloads, patch("services.memory_service.owner_email", return_value=OWNER), \
         patch("services.memory_service.owner_id", return_value=OWNER_ID):
        seams = type(ce.get_task_execution_service()).execute_task.__globals__
        seams["db"].get_execution.return_value = row
        asyncio.run(ce.get_task_execution_service().execute_task(
            agent_name=AGENT, message="Remember I prefer invoices as PDF.",
            triggered_by=row.triggered_by, source_user_email=row.source_user_email,
            execution_id=EXEC, timeout_seconds=900,
        ))
    return payloads


def test_push_wire_marks_a_non_owner_turn(chat_layer):
    payloads = _push(chat_layer, _row(source_user_email=PERSON))
    assert payloads and payloads[0].get("disable_auto_memory") is True


def test_push_wire_leaves_the_owner_turn_unchanged(chat_layer):
    payloads = _push(chat_layer, _row(source_user_email=OWNER))
    assert payloads and "disable_auto_memory" not in payloads[0]


# ---------------------------------------------------------------------------
# The /api/chat wire
# ---------------------------------------------------------------------------

def _chat_payload(ce, row, monkeypatch):
    monkeypatch.setattr(ce.db, "get_execution", lambda eid: row)
    monkeypatch.setattr(ce.db, "mark_execution_dispatched", lambda *a, **k: None)
    with patch("services.memory_service.owner_email", return_value=OWNER), \
         patch("services.memory_service.owner_id", return_value=OWNER_ID):
        return ce.build_chat_payload(
            name=AGENT,
            request=SimpleNamespace(message="Remember I prefer invoices as PDF.", model=None),
            triggered_by=row.triggered_by,
            current_user=SimpleNamespace(email=row.source_user_email, username="u", mcp_key_name=None),
            x_source_agent=None,
            task_execution_id=EXEC,
        )


def test_chat_wire_marks_a_shared_users_turn(chat_layer, monkeypatch):
    payload = _chat_payload(chat_layer, _row(triggered_by="chat", source_user_email=PERSON), monkeypatch)
    assert payload.get("disable_auto_memory") is True


def test_chat_wire_leaves_the_owner_turn_unchanged(chat_layer, monkeypatch):
    payload = _chat_payload(chat_layer, _row(triggered_by="chat", source_user_email=OWNER), monkeypatch)
    assert "disable_auto_memory" not in payload


# ---------------------------------------------------------------------------
# The pull claim envelope
# ---------------------------------------------------------------------------

def _claim_payload(source_user_email):
    from services import pull_coordination_service as pcs

    row = {
        "id": EXEC, "agent_name": AGENT, "message": "Remember I prefer invoices as PDF.",
        "backlog_metadata": json.dumps({"message": "m"}), "triggered_by": "public",
        "source_user_email": source_user_email,
        "model_used": None, "lease_expires_at": None, "claim_token": "t",
        "claimed_by_worker": "w", "redelivery_count": 0,
    }
    with patch.object(pcs, "_resolve_agent_runtime", return_value="claude-code"), \
         patch.object(pcs, "is_execution_context_enabled", return_value=False), \
         patch.object(pcs, "compose_system_prompt", side_effect=lambda **kw: kw["caller_prompt"]), \
         patch("services.memory_service.owner_email", return_value=OWNER), \
         patch("services.memory_service.owner_id", return_value=OWNER_ID):
        return pcs._build_claim_response(row)["envelope"]["payload"]


def test_pull_claim_marks_a_non_owner_turn():
    assert _claim_payload(PERSON).get("disable_auto_memory") is True


def test_pull_claim_leaves_the_owner_turn_unchanged():
    assert "disable_auto_memory" not in _claim_payload(OWNER)


# ---------------------------------------------------------------------------
# Review of #4172: the two spawns that reach the agent without `execute_task`
# ---------------------------------------------------------------------------

def _agent_client_payloads(monkeypatch):
    from services.agent_client import client as ac

    sent = []

    async def post(self, path, json=None, timeout=None):
        sent.append(json)
        return SimpleNamespace(status_code=200, json=lambda: {"response": "ok", "execution_log": [],
                                                              "metadata": {}, "session_id": "s"})
    monkeypatch.setattr(ac.AgentClient, "post", post)
    monkeypatch.setattr(ac.AgentClient, "_parse_task_response", lambda self, r: r)
    return ac, sent


def test_agent_client_task_sends_the_flag_only_when_asked(monkeypatch):
    ac, sent = _agent_client_payloads(monkeypatch)
    client = ac.AgentClient.__new__(ac.AgentClient)
    asyncio.run(client.task("m", timeout=10.0, disable_auto_memory=True))
    asyncio.run(client.task("m", timeout=10.0))
    assert sent[0].get("disable_auto_memory") is True
    assert "disable_auto_memory" not in sent[1]


def test_a_voice_tool_call_spawns_with_auto_memory_off(monkeypatch):
    """VoIP and the legacy voice tool reach the agent through `AgentClient.task`
    with the caller's speech as the prompt and no execution row to judge."""
    from unittest.mock import AsyncMock

    import services.agent_client as agent_client
    import services.skill_gate_service as sgs
    from services import gemini_voice as gv

    monkeypatch.setattr(sgs, "enforce", AsyncMock(return_value=None))
    client = MagicMock()
    client.task = AsyncMock(return_value=SimpleNamespace(response_text="done"))
    monkeypatch.setattr(agent_client, "get_agent_client", MagicMock(return_value=client))
    svc = gv.GeminiVoiceService.__new__(gv.GeminiVoiceService)
    assert asyncio.run(svc._execute_tool(AGENT, "run_task", {"prompt": "remember my PIN"})) == "done"
    assert client.task.await_args.kwargs.get("disable_auto_memory") is True


def test_the_public_intro_spawns_with_auto_memory_off(monkeypatch):
    from routers import public

    sent = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            sent.append(json)
            return SimpleNamespace(status_code=200, text="", json=lambda: {"response": "Hi"})

    monkeypatch.setattr(public, "check_public_link_rate_limit", lambda ip: None)
    monkeypatch.setattr(public, "_validate_public_link", lambda token: {"id": "l1", "agent_name": AGENT})
    monkeypatch.setattr(public.public_chat_service, "agent_requires_email", lambda name: False)
    monkeypatch.setattr(public, "get_agent_container", lambda name: SimpleNamespace(status="running"))
    monkeypatch.setattr(public, "agent_httpx_client", lambda name, timeout=None: _Client())
    req = SimpleNamespace(client=SimpleNamespace(host="203.0.113.9"), headers={})
    try:
        asyncio.run(public.get_agent_intro("tok", req))
    except Exception:
        pass  # the response shaping is not under test; the spawn payload is
    assert sent and sent[0].get("disable_auto_memory") is True
