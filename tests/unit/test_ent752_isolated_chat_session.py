"""
Gated skills, the in-container hook — a self-approved `/chat` turn runs in its
own session (trinity-enterprise#752, gate decision #40).

The agent's shared chat session is resumed by whoever chats next. A skill one
approver loaded into it would stay in context for the next caller — who could
then act on it with no approval of their own. So a self-approved `/chat` turn
is run fresh and is NOT kept as the agent's chat session; the next ordinary
turn resumes the shared session as it was.

Targets, through each layer the flag crosses:
  * the backend `/chat` router (`routers/chat.py::chat_with_agent`): the
    admission's decision reaches the row's setup, and the setup's verdict
    reaches the turn — driven for real up to `run_chat_turn`;
  * `chat_execution_service.build_chat_payload`: `isolated_session` on the
    agent's `/api/chat` payload only when asked;
  * the agent server's `/api/chat` router: passed to the runtime, and the
    shared session's stats left alone;
  * `claude_code.execute_claude_code` against the #2958 fake `claude`.
Related flow: docs/memory/feature-flows/skill-gate.md
"""
import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_2958_chat_session_isolation import M1, M2, shim  # noqa: E402,F401 — `shim` is a fixture

from agent_server.services import claude_code  # noqa: E402
from agent_server.state import agent_state  # noqa: E402

pytestmark = pytest.mark.unit


async def _turn(prompt, model=M1, **kw):
    _text, _log, metadata, _raw = await claude_code.execute_claude_code(
        prompt, model=model, execution_id=f"exec-{uuid.uuid4().hex[:12]}", **kw)
    return metadata


# ---------------------------------------------------------------------------
# The agent runtime
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_isolated_turn_starts_fresh_and_the_shared_session_continues_without_it(shim):
    first = await _turn("hello")
    shared = agent_state.chat_session_id
    assert shared == first.session_id

    isolated = await _turn("pay the invoice", isolated_session=True)
    assert isolated.session_id != shared
    assert "--resume" not in shim.runs()[-1]
    assert agent_state.chat_session_id == shared
    assert shim.marker_id() == shared

    after = await _turn("and then?")
    assert after.session_id == shared
    run = shim.runs()[-1]
    assert run[run.index("--resume") + 1] == shared


@pytest.mark.asyncio
async def test_an_isolated_turn_leaves_the_shared_model_and_counters_alone(shim):
    await _turn("hello", model=M1)
    agent_state.session_context_tokens = 123_000
    agent_state.session_total_cost = 1.5
    agent_state.session_total_output_tokens = 777

    await _turn("pay the invoice", model=M2, isolated_session=True)
    run = shim.runs()[-1]
    assert run[run.index("--model") + 1] == M2          # this turn ran on what it asked for
    assert agent_state.current_model == M1               # the shared choice did not move
    assert agent_state.chat_session_model == M1
    assert (agent_state.session_context_tokens, agent_state.session_total_cost,
            agent_state.session_total_output_tokens) == (123_000, 1.5, 777)


@pytest.mark.asyncio
async def test_an_isolated_turn_with_no_model_uses_the_shared_one_without_taking_it(shim):
    await _turn("hello", model=M1)
    await _turn("pay the invoice", model=None, isolated_session=True)
    run = shim.runs()[-1]
    assert run[run.index("--model") + 1] == M1
    assert agent_state.current_model == M1


# ---------------------------------------------------------------------------
# The agent server's /api/chat
# ---------------------------------------------------------------------------

class _Runtime:
    def __init__(self):
        self.calls = []

    async def execute(self, **kw):
        from agent_server.models import ExecutionMetadata
        self.calls.append(kw)
        return "ok", [], ExecutionMetadata(cost_usd=0.25, output_tokens=40, input_tokens=90_000,
                                           context_window=200_000), []


@pytest.fixture
def agent_chat(monkeypatch):
    from agent_server.routers import chat as router
    runtime = _Runtime()
    monkeypatch.setattr(router, "get_runtime", lambda: runtime)
    monkeypatch.setattr(router, "_retain_terminal", lambda *a, **k: None)
    monkeypatch.setattr(agent_state, "conversation_history", [])
    monkeypatch.setattr(agent_state, "session_total_cost", 2.0)
    monkeypatch.setattr(agent_state, "session_total_output_tokens", 100)
    monkeypatch.setattr(agent_state, "session_context_tokens", 9_000)
    monkeypatch.setattr(agent_state, "session_context_window", 200_000)
    return SimpleNamespace(router=router, runtime=runtime)


@pytest.mark.asyncio
async def test_the_agent_server_passes_isolation_to_the_runtime_and_keeps_the_shared_stats(agent_chat):
    from agent_server.models import ChatRequest
    await agent_chat.router.chat(ChatRequest(message="pay", execution_id="exec-iso-752",
                                             isolated_session=True))
    assert agent_chat.runtime.calls[-1]["isolated_session"] is True
    assert (agent_state.session_total_cost, agent_state.session_total_output_tokens,
            agent_state.session_context_tokens) == (2.0, 100, 9_000)


@pytest.mark.asyncio
async def test_an_ordinary_turn_is_unchanged(agent_chat):
    from agent_server.models import ChatRequest
    await agent_chat.router.chat(ChatRequest(message="hello", execution_id="exec-plain-752"))
    assert agent_chat.runtime.calls[-1]["isolated_session"] is False
    assert agent_state.session_total_cost == 2.25
    assert agent_state.session_context_tokens == 90_000


def test_an_older_backend_that_sends_no_flag_gets_an_ordinary_turn():
    from agent_server.models import ChatRequest
    assert ChatRequest(message="hi").isolated_session is False


def test_every_runtime_accepts_the_flag():
    """The router passes it to whatever runtime the agent runs; Gemini and
    Codex have no hook (D4) and ignore it, but must not reject the call."""
    import inspect
    from agent_server.services.claude_code import ClaudeCodeRuntime
    from agent_server.services.codex_runtime import CodexRuntime
    from agent_server.services.gemini_runtime import GeminiRuntime
    from agent_server.services.runtime_adapter import AgentRuntime
    for cls in (AgentRuntime, ClaudeCodeRuntime, GeminiRuntime, CodexRuntime):
        param = inspect.signature(cls.execute).parameters.get("isolated_session")
        assert param is not None and param.default is False, cls.__name__


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------

from db_harness import db_backend  # noqa: E402,F401
from test_ent751_gate_entries import FIN, OWNER_EMAIL, _human, world  # noqa: E402,F401

import services.chat_execution_service as _CE  # noqa: E402
from database import db  # noqa: E402
from models import ChatMessageRequest  # noqa: E402


@pytest.fixture
def payload_env(world, monkeypatch):
    import services.docker_service as ds
    monkeypatch.setattr(ds, "get_agent_runtime", lambda name: "claude-code")
    return world


@pytest.mark.parametrize("isolated, expected", [(True, True), (False, None)])
def test_the_payload_carries_isolation_only_when_asked(payload_env, isolated, expected):
    run = db.create_task_execution(agent_name=FIN, message="x", triggered_by="chat")
    payload = _CE.build_chat_payload(
        name=FIN, request=ChatMessageRequest(message="/pay-invoice 1"), triggered_by="chat",
        current_user=_human(payload_env), x_source_agent=None, task_execution_id=run.id,
        isolated_session=isolated)
    assert payload.get("isolated_session") is expected
    assert payload["execution_id"] == run.id


@pytest.mark.parametrize("isolated", [True, False])
def test_the_turn_sends_what_the_setup_decided(payload_env, monkeypatch, isolated):
    """`run_chat_turn` → `build_chat_payload` → the agent's /api/chat body."""
    run = db.create_task_execution(agent_name=FIN, message="x", triggered_by="chat")
    sent = {}

    class _Stop(Exception):
        pass

    async def _post(name, path, payload, **kw):
        sent.update(payload)
        raise _Stop()

    monkeypatch.setattr(_CE, "agent_post_with_retry", _post)
    from services import idempotency_service
    idem = idempotency_service.begin(idempotency_service.make_agent_scope(FIN), None)
    capacity = SimpleNamespace(release=AsyncMock())
    with pytest.raises(_Stop):
        asyncio.run(_CE.run_chat_turn(
            name=FIN, request=ChatMessageRequest(message="/pay-invoice 1"),
            current_user=_human(payload_env), x_source_agent=None, triggered_by="chat",
            task_execution_id=run.id, _chat_subscription_id=None, chat_activity_id=None,
            collaboration_activity_id=None, session=None, execution=SimpleNamespace(id="slot"),
            queue_result="running", is_queued=False, chat_timeout=60, idem=idem,
            capacity=capacity, isolated_session=isolated))
    assert sent.get("isolated_session") is (True if isolated else None)


@pytest.mark.parametrize("message, isolated", [
    pytest.param("/pay-invoice 100 EUR", True, id="self-approved"),
    pytest.param("hello there", False, id="ungated"),
])
def test_the_chat_router_carries_the_decision_to_the_turn(payload_env, monkeypatch, message, isolated):
    """The router's two call sites: the admission's decision reaches the row's
    setup (which records the clearance), and the setup's verdict reaches the
    turn (which isolates it). Driven for real up to `run_chat_turn`."""
    from routers import chat as router
    monkeypatch.setattr(router, "get_agent_container", lambda name: SimpleNamespace(status="running"))
    monkeypatch.setattr(_CE.activity_service, "track_activity", AsyncMock(return_value="act-752"))
    seen = {}

    async def _turn(**kw):
        seen.update(kw)
        return {"response": "ok"}

    monkeypatch.setattr(_CE, "run_chat_turn", _turn)
    asyncio.run(router.chat_with_agent(
        request=ChatMessageRequest(message=message), name=FIN,
        current_user=_human(payload_env), x_source_agent=None, x_via_mcp=None,
        idempotency_key=f"idem-router-{isolated}", x_trinity_execution_id=None))
    assert seen["isolated_session"] is isolated
    record = db.get_gate_request_by_dispatched_execution(seen["task_execution_id"])
    if isolated:
        assert (record["state"], record["requester_email"]) == ("self_approved", OWNER_EMAIL)
    else:
        assert record is None
