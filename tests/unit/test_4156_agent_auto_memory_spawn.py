"""#4156 — a turn that serves a person spawns Claude Code with auto-memory OFF.

The bug: on a Workspace turn a non-owner said "Remember I prefer invoices as
PDF", the agent replied "Saved", and the note went to Claude Code's
auto-memory (`~/.claude/projects/<project>/memory/`) — one directory every
user of the agent shares — instead of `write_user_memory`.

Root cause (read off the pinned Claude Code binary, not guessed): auto-memory
is on by default, and its system prompt tells the model "If the user
explicitly asks you to remember something, save it immediately". That is a
harness instruction for exactly the person's sentence, and it competes with
Trinity's appended "use write_user_memory" section on every such turn. The
same binary reads `CLAUDE_CODE_DISABLE_AUTO_MEMORY` (truthy: 1/true/yes/on)
and, when set, takes the memory system out of the turn — no instruction, no
read of the shared directory, no write to it.

So the fix is structural, not a prompt edit: the backend marks a turn that
serves someone other than the owner (`disable_auto_memory`), and the agent
puts `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in THAT spawn's env. These tests pin
the outcome at the spawn — the env the `claude` process is started with —
for both spawn paths (headless `/api/task` and `/api/chat`), and that the
owner's / a headless run's spawn is unchanged.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_server.services import claude_code as cc
from agent_server.services import execution_env as ee
from agent_server.services import headless_executor as he
from agent_server.services import runtime_adapter as ra

pytestmark = pytest.mark.unit

KEY = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"


class _Spawned(Exception):
    """Raised by the fake Popen once it has recorded the spawn's env."""


@pytest.fixture
def captured_env(monkeypatch):
    """Record the env of the next `subprocess.Popen` and stop the run there.

    The key is removed from the container baseline so a developer's shell (or
    a future image default) cannot decide the owner-turn case."""
    monkeypatch.delitem(ee.INITIAL_ENV, KEY, raising=False)
    seen: dict = {}

    def _popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env") or {}
        raise _Spawned()

    monkeypatch.setattr("subprocess.Popen", _popen)
    return seen


def _headless_ctx(**kw):
    ctx = he._setup_headless_command(
        prompt="Remember I prefer invoices as PDF.",
        model="sonnet",
        allowed_tools=None,
        system_prompt=None,
        timeout_seconds=60,
        max_turns=None,
        execution_id="exec-4156",
        resume_session_id=None,
        persist_session=False,
        images=None,
        **kw,
    )
    return ctx


def _run_headless(monkeypatch, ctx):
    registry = MagicMock()
    registry.was_terminated.return_value = False
    monkeypatch.setattr(he, "get_process_registry", lambda: registry)
    with pytest.raises(_Spawned):
        he._run_headless_subprocess(ctx)


# ---------------------------------------------------------------------------
# The headless spawn (/api/task, the async 202 path, the pull worker)
# ---------------------------------------------------------------------------

def test_headless_person_turn_spawns_with_auto_memory_off(monkeypatch, captured_env):
    _run_headless(monkeypatch, _headless_ctx(disable_auto_memory=True))
    assert captured_env["env"].get(KEY) == "1"


def test_headless_owner_turn_spawn_is_unchanged(monkeypatch, captured_env):
    _run_headless(monkeypatch, _headless_ctx())
    assert KEY not in captured_env["env"]


def test_a_dotenv_value_cannot_turn_auto_memory_back_on(monkeypatch, captured_env, tmp_path):
    """`.env` is re-read at every spawn and an agent can write it. A falsy
    value there means "forced on" to Claude Code, so the per-spawn layer must
    sit above the file."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY}=0\n")
    monkeypatch.setattr(ee, "ENV_FILE", env_file)
    real = ee.build_execution_env
    monkeypatch.setattr(he, "build_execution_env",
                        lambda extra=None, **kw: real(extra, env_file=env_file, **kw))
    _run_headless(monkeypatch, _headless_ctx(disable_auto_memory=True))
    assert captured_env["env"].get(KEY) == "1"


# ---------------------------------------------------------------------------
# The chat spawn (/api/chat)
# ---------------------------------------------------------------------------

def _run_chat(monkeypatch, **kw):
    monkeypatch.setattr(cc, "_load_guardrails", lambda: {})
    # The fake Popen stops the run; the chat path may wrap that in its own error.
    with pytest.raises(Exception):
        asyncio.run(cc._execute_claude_code_once(
            "Remember I prefer invoices as PDF.", False, "sonnet", None,
            "exec-4156-chat", None, {}, **kw,
        ))


def test_chat_person_turn_spawns_with_auto_memory_off(monkeypatch, captured_env):
    _run_chat(monkeypatch, disable_auto_memory=True)
    assert captured_env["env"].get(KEY) == "1"


def test_chat_owner_turn_spawn_is_unchanged(monkeypatch, captured_env):
    _run_chat(monkeypatch)
    assert "env" in captured_env, "the chat path never reached its spawn"
    assert KEY not in captured_env["env"]


# ---------------------------------------------------------------------------
# The request → runtime hop, for every entry point
# ---------------------------------------------------------------------------

class _Recorder:
    supports_auth_override = True
    supports_auto_memory_switch = True

    def __init__(self):
        self.calls = []

    async def execute_headless(self, **kw):
        self.calls.append(kw)
        return "ok", [], SimpleNamespace(session_id="s", cost_usd=None, error_type=None), "s"


def test_spawn_kwargs_reach_only_a_runtime_that_has_the_switch():
    assert ee.auto_memory_spawn_kwargs(_Recorder(), True) == {"disable_auto_memory": True}
    assert ee.auto_memory_spawn_kwargs(_Recorder(), False) == {}
    # Gemini / Codex have no Claude auto-memory and never take the keyword.
    assert ee.auto_memory_spawn_kwargs(SimpleNamespace(), True) == {}


def test_claude_runtime_declares_the_switch():
    assert cc.ClaudeCodeRuntime.supports_auto_memory_switch is True
    assert ra.AgentRuntime.supports_auto_memory_switch is False


def test_both_request_models_carry_the_flag_and_default_off():
    from agent_server.models import ChatRequest, ParallelTaskRequest

    assert ChatRequest(message="m").disable_auto_memory is False
    assert ParallelTaskRequest(message="m").disable_auto_memory is False
    assert ChatRequest(message="m", disable_auto_memory=True).disable_auto_memory is True
    assert ParallelTaskRequest(message="m", disable_auto_memory=True).disable_auto_memory is True


@pytest.mark.parametrize("module_name, needle", [
    ("routers/chat.py", 'auto_memory_spawn_kwargs(runtime, getattr(request, "disable_auto_memory", False))'),
    ("services/result_callback.py", 'auto_memory_spawn_kwargs(runtime, getattr(request, "disable_auto_memory", False))'),
    ("services/pull_worker.py", "auto_memory_spawn_kwargs(runtime, payload.get(\"disable_auto_memory\"))"),
])
def test_every_entry_point_forwards_the_flag(module_name, needle):
    """/api/chat, /api/task (sync), the #1083 async path and the pull worker
    all reach the runtime; each must hand the flag on, or that path leaks."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "docker" / "base-image" / "agent_server" / module_name).read_text()
    assert needle in src


def test_async_result_path_forwards_the_flag(monkeypatch):
    from agent_server.models import ParallelTaskRequest
    from agent_server.services import result_callback as rc

    runtime = _Recorder()
    monkeypatch.setattr(ra, "get_runtime", lambda: runtime)
    monkeypatch.setattr(rc, "_deliver", MagicMock(), raising=False)
    req = ParallelTaskRequest(message="m", execution_id="exec-4156-async", disable_auto_memory=True)
    try:
        asyncio.run(rc._run_and_report(req, "http://backend", "k", 0.0))
    except Exception:
        pass   # delivery is not under test; the spawn call is
    assert runtime.calls and runtime.calls[0].get("disable_auto_memory") is True


# ---------------------------------------------------------------------------
# Review of #4172: the hand-offs, executed (a source grep stayed green with
# either `routers/chat.py` line deleted, and with the cold-retry or isolated
# forwarding in `claude_code.py` dropped)
# ---------------------------------------------------------------------------

class _Stop(Exception):
    """Raised by the recording runtime once it has the call's kwargs."""


class _StopRuntime:
    supports_auth_override = False
    supports_auto_memory_switch = True

    def __init__(self):
        self.calls = []

    async def execute(self, **kw):
        self.calls.append(kw)
        raise _Stop()

    async def execute_headless(self, **kw):
        self.calls.append(kw)
        raise _Stop()


@pytest.fixture
def chat_router(monkeypatch):
    from agent_server.routers import chat as chat_mod

    runtime = _StopRuntime()
    monkeypatch.setattr(chat_mod, "get_runtime", lambda: runtime)
    monkeypatch.setattr(chat_mod, "get_process_registry", lambda: MagicMock())
    monkeypatch.setattr(chat_mod.result_callback, "try_spawn_async", lambda request: False)
    monkeypatch.setattr(chat_mod.agent_state, "add_message", lambda *a, **k: None)
    monkeypatch.setattr(chat_mod.agent_state, "record_task_start", lambda *a, **k: None)
    monkeypatch.setattr(chat_mod.agent_state, "record_task_finish", lambda *a, **k: None)
    return chat_mod, runtime


@pytest.mark.parametrize("flag", [True, False])
def test_the_chat_route_hands_the_flag_to_the_runtime(chat_router, flag):
    from agent_server.models import ChatRequest

    chat_mod, runtime = chat_router
    with pytest.raises(_Stop):
        asyncio.run(chat_mod.chat(ChatRequest(message="m", execution_id="e1", disable_auto_memory=flag)))
    assert runtime.calls[0].get("disable_auto_memory", False) is flag


@pytest.mark.parametrize("flag", [True, False])
def test_the_task_route_hands_the_flag_to_the_runtime(chat_router, flag):
    from agent_server.models import ParallelTaskRequest

    chat_mod, runtime = chat_router
    with pytest.raises(_Stop):
        asyncio.run(chat_mod.execute_task(
            ParallelTaskRequest(message="m", execution_id="e2", disable_auto_memory=flag)))
    assert runtime.calls[0].get("disable_auto_memory", False) is flag


@pytest.fixture
def once_calls(monkeypatch):
    """`execute_claude_code` with the one-subprocess step recorded, not run."""
    calls = []
    monkeypatch.setattr(cc.agent_state, "claude_code_available", True)
    monkeypatch.setattr(cc, "_capture_chat_session", lambda *a, **k: None)
    return calls


def test_an_isolated_session_turn_keeps_the_flag(monkeypatch, once_calls):
    async def once(*a, **kw):
        once_calls.append(kw)
        return "ok", [], SimpleNamespace(), []
    monkeypatch.setattr(cc, "_execute_claude_code_once", once)
    asyncio.run(cc.execute_claude_code("m", model="sonnet", execution_id="e3",
                                       isolated_session=True, disable_auto_memory=True))
    assert [c.get("disable_auto_memory") for c in once_calls] == [True]


def test_the_cold_retry_keeps_the_flag(monkeypatch, once_calls):
    """A resume whose JSONL is gone gets one cold retry; that second spawn is
    the same person's turn and must keep auto-memory off."""
    from fastapi import HTTPException

    sid = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(cc.agent_state, "chat_session_id", sid)
    monkeypatch.setattr(cc.agent_state, "current_model", "sonnet")
    monkeypatch.setattr(cc.agent_state, "chat_session_model", "sonnet")
    monkeypatch.setattr(cc, "_valid_session_id", lambda s: s)
    monkeypatch.setattr(cc, "_cold_retry_reason", lambda *a, **k: "resume_jsonl_missing")
    registry = MagicMock()
    registry.was_terminated.return_value = False
    monkeypatch.setattr(cc, "get_process_registry", lambda: registry)
    monkeypatch.setattr(cc.chat_session_marker, "clear", lambda: None)

    async def once(*a, **kw):
        once_calls.append(kw)
        if len(once_calls) == 1:
            raise HTTPException(status_code=500, detail="resume failed")
        return "ok", [], SimpleNamespace(), []
    monkeypatch.setattr(cc, "_execute_claude_code_once", once)
    asyncio.run(cc.execute_claude_code("m", model="sonnet", execution_id="e4", disable_auto_memory=True))
    assert [c.get("disable_auto_memory") for c in once_calls] == [True, True]


# ---------------------------------------------------------------------------
# The whole hand-off, from the runtime's public method to the process env.
# Every test above enters past at least one of these joins, so dropping the
# keyword at any of them left the suite green while `/api/task` lost the fix.
# ---------------------------------------------------------------------------

def _runtime_turn(monkeypatch, method, **kw):
    registry = MagicMock()
    registry.was_terminated.return_value = False
    monkeypatch.setattr(he, "get_process_registry", lambda: registry)
    monkeypatch.setattr(cc, "get_process_registry", lambda: registry, raising=False)
    monkeypatch.setattr(cc, "_load_guardrails", lambda: {})
    runtime = cc.ClaudeCodeRuntime()
    # The fake Popen stops the run; each path may wrap that in its own error.
    with pytest.raises(Exception):
        asyncio.run(getattr(runtime, method)(prompt="Remember I prefer invoices as PDF.", model="sonnet",
                                             execution_id="exec-4156-hop", **kw))


@pytest.mark.parametrize("method, extra", [("execute_headless", {"timeout_seconds": 60}), ("execute", {})])
@pytest.mark.parametrize("flag", [True, False])
def test_the_runtime_method_carries_the_flag_to_the_spawn(monkeypatch, captured_env, method, extra, flag):
    _runtime_turn(monkeypatch, method, disable_auto_memory=flag, **extra)
    assert "env" in captured_env, "the turn never reached the spawn"
    assert (captured_env["env"].get(KEY) == "1") is flag


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {"applied": True}


class _Client:
    async def post(self, *_a, **_k):
        return _Resp()


@pytest.mark.parametrize("flag", [True, False])
def test_a_pulled_turn_carries_the_flag_to_the_spawn(monkeypatch, captured_env, flag):
    from agent_server.services import pull_worker as pw

    registry = MagicMock()
    registry.was_terminated.return_value = False
    monkeypatch.setattr(he, "get_process_registry", lambda: registry)
    monkeypatch.setattr("agent_server.services.process_registry.get_process_registry", lambda: registry)
    monkeypatch.setattr(ra, "get_runtime", lambda: cc.ClaudeCodeRuntime())
    claim = {
        "execution_id": "exec-4156-pull", "claim_token": "tok",
        "envelope": {"payload": {"message": "Remember I prefer invoices as PDF.", "session_id": None,
                                 "disable_auto_memory": flag}},
    }
    asyncio.run(pw._run_and_report(claim, _Client(), "http://backend:8000", "key", "alpha"))
    assert "env" in captured_env, "the turn never reached the spawn"
    assert (captured_env["env"].get(KEY) == "1") is flag
