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
