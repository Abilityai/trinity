"""
Unit tests for voice tool call support (#581).

Tests GeminiVoiceService tool execution routing without a real Gemini
connection, agent container, or database.

Feature: VOICE-001 tool calls
Issue: https://github.com/abilityai/trinity/issues/581
"""

import asyncio
import json
import sys
import types
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _run(coro):
    return asyncio.run(coro)


# ── Stub heavy dependencies so we can import gemini_voice in isolation ────────

def _stub_genai():
    """Provide a minimal google.genai stub."""
    google = types.ModuleType("google")
    genai = types.ModuleType("google.genai")
    gtypes = types.ModuleType("google.genai.types")

    class _FunctionDeclaration:
        def __init__(self, **kw): self.__dict__.update(kw)

    class _Schema:
        OBJECT = "OBJECT"
        STRING = "STRING"
        def __init__(self, **kw): self.__dict__.update(kw)

    class _Type:
        OBJECT = "OBJECT"
        STRING = "STRING"

    class _Tool:
        def __init__(self, **kw): self.__dict__.update(kw)

    class _SpeechConfig:
        def __init__(self, **kw): self.__dict__.update(kw)

    class _VoiceConfig:
        def __init__(self, **kw): self.__dict__.update(kw)

    class _PrebuiltVoiceConfig:
        def __init__(self, **kw): self.__dict__.update(kw)

    class _LiveConnectConfig:
        def __init__(self, **kw): self.__dict__.update(kw)

    class _FunctionResponse:
        def __init__(self, **kw): self.__dict__.update(kw)

    gtypes.FunctionDeclaration = _FunctionDeclaration
    gtypes.Schema = _Schema
    gtypes.Type = _Type
    gtypes.Tool = _Tool
    gtypes.SpeechConfig = _SpeechConfig
    gtypes.VoiceConfig = _VoiceConfig
    gtypes.PrebuiltVoiceConfig = _PrebuiltVoiceConfig
    gtypes.LiveConnectConfig = _LiveConnectConfig
    gtypes.FunctionResponse = _FunctionResponse

    class _Client:
        def __init__(self, api_key=None): pass
        aio = MagicMock()

    genai.Client = _Client
    genai.types = gtypes
    google.genai = genai

    sys.modules["google"] = google
    sys.modules["google.genai"] = genai
    sys.modules["google.genai.types"] = gtypes


def _stub_config():
    config_mod = types.ModuleType("config")
    config_mod.GEMINI_API_KEY = "test-key"
    config_mod.VOICE_MODEL = "test-model"
    config_mod.VOICE_MAX_DURATION = 300
    config_mod.REDIS_URL = "redis://user:pass@localhost:6379"
    config_mod.DEFAULT_GITHUB_TEMPLATE_REPOS = []
    config_mod.GITHUB_PAT_CREDENTIAL_ID = "github-pat-templates"
    # Required by dependencies.py when test_voice_auth.py runs in the same session
    config_mod.SECRET_KEY = "test-secret-key-for-unit-tests"
    config_mod.ALGORITHM = "HS256"
    config_mod.VOICE_ENABLED = True
    sys.modules["config"] = config_mod


def _stub_services_package():
    """Pre-stub services sub-modules that services/__init__.py imports."""
    docker_svc = types.ModuleType("services.docker_service")
    docker_svc.docker_client = None
    docker_svc.get_agent_container = MagicMock(return_value=None)
    docker_svc.get_agent_status_from_container = MagicMock()
    docker_svc.list_all_agents = MagicMock(return_value=[])
    docker_svc.get_agent_by_name = MagicMock(return_value=None)
    docker_svc.get_next_available_port = MagicMock(return_value=2222)
    sys.modules["services.docker_service"] = docker_svc

    tmpl_svc = types.ModuleType("services.template_service")
    tmpl_svc.get_github_template = MagicMock()
    tmpl_svc.clone_github_repo = MagicMock()
    tmpl_svc.extract_agent_credentials = MagicMock()
    tmpl_svc.generate_credential_files = MagicMock()
    sys.modules["services.template_service"] = tmpl_svc


_stub_genai()
_stub_config()
_stub_services_package()

# Now we can import the service
from services.gemini_voice import (  # noqa: E402
    GeminiVoiceService, VoiceSession, _TOOL_PROMPT_MAX, _PANEL_CONTENT_MAX,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_session(agent_name="test-agent") -> VoiceSession:
    return VoiceSession(
        session_id="vs_test",
        agent_name=agent_name,
        chat_session_id="cs_test",
        user_id=1,
        user_email="user@example.com",
        system_prompt="You are a test agent.",
    )


@pytest.fixture
def svc():
    return GeminiVoiceService()


# ── Tests: _execute_tool ──────────────────────────────────────────────────────

class TestExecuteTool:

    def test_success(self, svc):
        """Patching _execute_tool itself returns the expected value."""
        with patch.object(svc, "_execute_tool", AsyncMock(return_value="The answer is 42.")):
            result = _run(svc._execute_tool("test-agent", "run_task", {"prompt": "What is 42?"}))
        assert result == "The answer is 42."

    def test_success_real(self, svc):
        """Test _execute_tool with a mocked get_agent_client."""
        mock_response = MagicMock()
        mock_response.response = "Task result text."
        mock_client = MagicMock()
        mock_client.task = AsyncMock(return_value=mock_response)

        # Patch inside services.agent_client (the import target)
        sys.modules.setdefault("services", types.ModuleType("services"))
        agent_client_mod = types.ModuleType("services.agent_client")

        class AgentNotReachableError(Exception): pass
        class AgentRequestError(Exception): pass

        agent_client_mod.get_agent_client = lambda name: mock_client
        agent_client_mod.AgentNotReachableError = AgentNotReachableError
        agent_client_mod.AgentRequestError = AgentRequestError
        sys.modules["services.agent_client"] = agent_client_mod

        result = _run(svc._execute_tool("test-agent", "run_task", {"prompt": "Do the thing"}))
        assert result == "Task result text."
        mock_client.task.assert_awaited_once()

    def test_empty_prompt(self, svc):
        sys.modules.setdefault("services", types.ModuleType("services"))
        agent_client_mod = types.ModuleType("services.agent_client")
        agent_client_mod.get_agent_client = MagicMock()
        agent_client_mod.AgentNotReachableError = Exception
        agent_client_mod.AgentRequestError = Exception
        sys.modules["services.agent_client"] = agent_client_mod

        result = _run(svc._execute_tool("test-agent", "run_task", {}))
        assert "No prompt" in result
        agent_client_mod.get_agent_client.assert_not_called()

    def test_prompt_truncated_to_max(self, svc):
        """Prompts longer than _TOOL_PROMPT_MAX are truncated before forwarding."""
        mock_response = MagicMock()
        mock_response.response = "ok"
        mock_client = MagicMock()
        mock_client.task = AsyncMock(return_value=mock_response)

        agent_client_mod = types.ModuleType("services.agent_client")
        agent_client_mod.get_agent_client = lambda name: mock_client
        agent_client_mod.AgentNotReachableError = Exception
        agent_client_mod.AgentRequestError = Exception
        sys.modules["services.agent_client"] = agent_client_mod

        long_prompt = "x" * (_TOOL_PROMPT_MAX + 500)
        _run(svc._execute_tool("test-agent", "run_task", {"prompt": long_prompt}))
        call_args = mock_client.task.call_args
        sent_prompt = call_args[0][0]
        assert len(sent_prompt) <= _TOOL_PROMPT_MAX + 3  # +3 for "..."

    def test_agent_not_reachable(self, svc):
        class AgentNotReachableError(Exception): pass
        class AgentRequestError(Exception): pass

        agent_client_mod = types.ModuleType("services.agent_client")
        agent_client_mod.AgentNotReachableError = AgentNotReachableError
        agent_client_mod.AgentRequestError = AgentRequestError

        def raise_unreachable(name):
            mock = MagicMock()
            mock.task = AsyncMock(side_effect=AgentNotReachableError("down"))
            return mock
        agent_client_mod.get_agent_client = raise_unreachable
        sys.modules["services.agent_client"] = agent_client_mod

        result = _run(svc._execute_tool("test-agent", "run_task", {"prompt": "hello"}))
        assert "not currently running" in result

    def test_task_error(self, svc):
        class AgentNotReachableError(Exception): pass
        class AgentRequestError(Exception): pass

        agent_client_mod = types.ModuleType("services.agent_client")
        agent_client_mod.AgentNotReachableError = AgentNotReachableError
        agent_client_mod.AgentRequestError = AgentRequestError

        def raise_request_error(name):
            mock = MagicMock()
            mock.task = AsyncMock(side_effect=AgentRequestError("500 bad"))
            return mock
        agent_client_mod.get_agent_client = raise_request_error
        sys.modules["services.agent_client"] = agent_client_mod

        result = _run(svc._execute_tool("test-agent", "run_task", {"prompt": "hello"}))
        assert "Task error" in result


# ── Tests: _execute_and_respond ───────────────────────────────────────────────

class TestExecuteAndRespond:

    def _make_fc(self, call_id="fc_1", name="run_task", args=None):
        fc = MagicMock()
        fc.id = call_id
        fc.name = name
        fc.args = args or {"prompt": "test prompt"}
        return fc

    def test_sends_tool_response_on_success(self, svc):
        session = _make_session()
        session._active = True
        gemini_session = MagicMock()
        gemini_session.send_tool_response = AsyncMock()
        session._gemini_session = gemini_session

        tool_call_cb = AsyncMock()
        tool_result_cb = AsyncMock()
        session._on_tool_call = tool_call_cb
        session._on_tool_result = tool_result_cb

        fc = self._make_fc()

        # Patch _execute_tool to return immediately
        with patch.object(svc, "_execute_tool", AsyncMock(return_value="42 is the answer")):
            _run(svc._execute_and_respond(session, "fc_1", fc))

        tool_call_cb.assert_awaited_once_with("run_task", {"prompt": "test prompt"})
        tool_result_cb.assert_awaited_once_with("run_task", "42 is the answer")
        gemini_session.send_tool_response.assert_awaited_once()
        # call_id removed from pending tasks after completion
        assert "fc_1" not in session._pending_tool_tasks

    def test_timeout_sends_error_response(self, svc):
        session = _make_session()
        session._active = True
        gemini_session = MagicMock()
        gemini_session.send_tool_response = AsyncMock()
        session._gemini_session = gemini_session
        session._on_tool_call = None
        session._on_tool_result = None

        fc = self._make_fc()

        async def slow(*a, **kw):
            await asyncio.sleep(100)

        with patch.object(svc, "_execute_tool", slow):
            with patch("services.gemini_voice.asyncio.wait_for",
                       AsyncMock(side_effect=asyncio.TimeoutError)):
                _run(svc._execute_and_respond(session, "fc_1", fc))

        gemini_session.send_tool_response.assert_awaited_once()
        # Response should contain timeout message
        call_kw = gemini_session.send_tool_response.call_args
        responses = call_kw[1].get("function_responses", call_kw[0][0] if call_kw[0] else [])
        if responses:
            resp = responses[0]
            assert "timed out" in str(getattr(resp, "response", {}).get("output", "")).lower()

    def test_inactive_session_skips_gemini_send(self, svc):
        session = _make_session()
        session._active = False
        gemini_session = MagicMock()
        gemini_session.send_tool_response = AsyncMock()
        session._gemini_session = gemini_session
        session._on_tool_call = None
        session._on_tool_result = None

        fc = self._make_fc()

        with patch.object(svc, "_execute_tool", AsyncMock(return_value="result")):
            _run(svc._execute_and_respond(session, "fc_1", fc))

        gemini_session.send_tool_response.assert_not_awaited()


# ── Tests: tool declaration presence ─────────────────────────────────────────

class TestToolDeclaration:

    def test_run_task_declared(self):
        from services.gemini_voice import _RUN_TASK_TOOL
        fds = _RUN_TASK_TOOL.function_declarations
        assert len(fds) == 1
        fd = fds[0]
        assert fd.name == "run_task"
        assert "prompt" in fd.parameters.properties

    def test_prompt_required(self):
        from services.gemini_voice import _RUN_TASK_TOOL
        fd = _RUN_TASK_TOOL.function_declarations[0]
        assert "prompt" in (fd.parameters.required or [])


# ── Tests: _execute_panel_tool ────────────────────────────────────────────────

class TestExecutePanelTool:
    """Tests for in-process panel tool execution (workspace mode canvas)."""

    def test_show_markdown_sets_state(self, svc):
        session = _make_session()
        result = svc._execute_panel_tool(session, "show_markdown", {"content": "# Hello"})
        assert result == "Panel updated."
        assert session.panel_state["type"] == "markdown"
        assert session.panel_state["content"] == "# Hello"
        assert session.panel_state["title"] is None
        assert session.panel_state["updated_at"] is not None

    def test_show_markdown_with_title(self, svc):
        session = _make_session()
        svc._execute_panel_tool(session, "show_markdown", {"content": "body", "title": "My Title"})
        assert session.panel_state["title"] == "My Title"

    def test_update_panel_sets_html(self, svc):
        session = _make_session()
        svc._execute_panel_tool(session, "update_panel", {"html": "<b>bold</b>", "title": "Report"})
        assert session.panel_state["type"] == "html"
        assert session.panel_state["content"] == "<b>bold</b>"
        assert session.panel_state["title"] == "Report"

    def test_append_to_panel_concatenates(self, svc):
        session = _make_session()
        session.panel_state = {"type": "html", "content": "A", "title": None, "updated_at": None}
        svc._execute_panel_tool(session, "append_to_panel", {"html": "B"})
        assert session.panel_state["content"] == "AB"
        assert session.panel_state["type"] == "html"

    def test_append_to_panel_caps_at_max(self, svc):
        session = _make_session()
        # Pre-fill content just under the limit, then append enough to exceed it
        session.panel_state = {
            "type": "html",
            "content": "x" * (_PANEL_CONTENT_MAX - 10),
            "title": None,
            "updated_at": None,
        }
        svc._execute_panel_tool(session, "append_to_panel", {"html": "y" * 100})
        assert len(session.panel_state["content"]) == _PANEL_CONTENT_MAX
        # The tail of the content should end with the appended "y"s
        assert session.panel_state["content"].endswith("y" * 10)

    def test_clear_panel_resets_state(self, svc):
        session = _make_session()
        session.panel_state = {"type": "html", "content": "old", "title": "t", "updated_at": "ts"}
        svc._execute_panel_tool(session, "clear_panel", {})
        assert session.panel_state["type"] == "empty"
        assert session.panel_state["content"] == ""
        assert session.panel_state["title"] is None

    def test_panel_tool_routed_not_forwarded_to_agent(self, svc):
        """Panel tools must not reach _execute_tool (no agent container call)."""
        session = _make_session()
        session._active = True
        gemini_session = MagicMock()
        gemini_session.send_tool_response = AsyncMock()
        session._gemini_session = gemini_session
        session._on_tool_call = None
        session._on_tool_result = None

        fc = MagicMock()
        fc.id = "fc_panel"
        fc.name = "show_markdown"
        fc.args = {"content": "# Test"}

        with patch.object(svc, "_execute_tool", AsyncMock()) as mock_exec:
            _run(svc._execute_and_respond(session, "fc_panel", fc))
            mock_exec.assert_not_awaited()

        assert session.panel_state["type"] == "markdown"
        gemini_session.send_tool_response.assert_awaited_once()


# ── Tests: end_session cancels pending tool tasks ─────────────────────────────

class TestEndSession:

    def test_cancels_pending_tool_tasks(self, svc):
        session = _make_session()
        session._active = True
        session._audio_in_queue = asyncio.Queue()
        session._gemini_session = None

        # Simulate a running tool task
        async def never_finish():
            await asyncio.sleep(9999)

        async def run():
            task = asyncio.create_task(never_finish())
            session._pending_tool_tasks["fc_abc"] = task
            svc._sessions[session.session_id] = session
            await svc.end_session(session.session_id)
            return task

        task = _run(run())
        assert task.cancelled() or task.done()
        assert len(session._pending_tool_tasks) == 0


# ── Tests: Redis cross-worker session fallback (#704) ────────────────────────

class TestRedisSessionFallback:
    """
    Tests for get_session() Redis cross-worker fallback (fix for #704).

    With --workers 2, POST /voice/start stores the session in Worker A's
    _sessions dict.  The subsequent WebSocket may hit Worker B, which has an
    empty _sessions.  get_session() now falls back to Redis metadata and
    reconstructs a VoiceSession so the ownership gate works on any worker.
    """

    def _make_redis_mock(self, return_value=None, side_effect=None):
        redis_mock = AsyncMock()
        if side_effect:
            redis_mock.get = AsyncMock(side_effect=side_effect)
        else:
            redis_mock.get = AsyncMock(return_value=return_value)
        redis_mock.setex = AsyncMock()
        redis_mock.delete = AsyncMock()
        return redis_mock

    def test_get_session_returns_in_memory_first(self, svc):
        """In-memory session is returned directly without a Redis call."""
        session = _make_session()
        svc._sessions[session.session_id] = session

        redis_mock = self._make_redis_mock(return_value=None)
        svc._get_redis = AsyncMock(return_value=redis_mock)

        result = _run(svc.get_session(session.session_id))
        assert result is session
        redis_mock.get.assert_not_awaited()

    def test_get_session_falls_back_to_redis(self, svc):
        """Session absent from memory is reconstructed from Redis metadata."""
        metadata = {
            "session_id": "vs_remote",
            "agent_name": "remote-agent",
            "chat_session_id": "cs_remote",
            "user_id": 42,
            "user_email": "remote@example.com",
            "voice_name": "Puck",
            "workspace_mode": True,
            "system_prompt": "You are remote.",
        }
        redis_mock = self._make_redis_mock(return_value=json.dumps(metadata))
        svc._get_redis = AsyncMock(return_value=redis_mock)

        result = _run(svc.get_session("vs_remote"))

        assert result is not None
        assert result.session_id == "vs_remote"
        assert result.agent_name == "remote-agent"
        assert result.user_id == 42
        assert result.workspace_mode is True
        # Stored in memory so subsequent calls skip Redis
        assert svc._sessions.get("vs_remote") is result

    def test_get_session_redis_miss_returns_none(self, svc):
        """Redis returning None (key expired/missing) → get_session() returns None."""
        redis_mock = self._make_redis_mock(return_value=None)
        svc._get_redis = AsyncMock(return_value=redis_mock)

        result = _run(svc.get_session("vs_missing"))
        assert result is None

    def test_get_session_redis_error_returns_none(self, svc):
        """Redis connection failure is swallowed; get_session() degrades to None."""
        redis_mock = self._make_redis_mock(side_effect=Exception("Redis unreachable"))
        svc._get_redis = AsyncMock(return_value=redis_mock)

        result = _run(svc.get_session("vs_error"))
        assert result is None

    def test_remove_session_deletes_redis_key(self, svc):
        """remove_session() deletes the Redis key in addition to clearing in-memory state."""
        session = _make_session()
        svc._sessions[session.session_id] = session

        redis_mock = self._make_redis_mock()
        svc._get_redis = AsyncMock(return_value=redis_mock)

        _run(svc.remove_session(session.session_id))

        assert session.session_id not in svc._sessions
        redis_mock.delete.assert_awaited_once_with(f"voice_session:{session.session_id}")

    def test_create_session_writes_redis(self, svc):
        """create_session() writes metadata to Redis with TTL = VOICE_MAX_DURATION + 60."""
        # Import constant from config stub (avoids cross-session stub collision with
        # test_voice_auth.py which replaces services.gemini_voice in sys.modules)
        import config as _cfg
        expected_ttl = _cfg.VOICE_MAX_DURATION + 60

        redis_mock = self._make_redis_mock()
        svc._get_redis = AsyncMock(return_value=redis_mock)

        session = _run(svc.create_session(
            agent_name="my-agent",
            chat_session_id="cs_1",
            user_id=7,
            user_email="test@example.com",
            system_prompt="Be helpful.",
        ))

        redis_mock.setex.assert_awaited_once()
        key, ttl, value = redis_mock.setex.call_args[0]
        assert key == f"voice_session:{session.session_id}"
        assert ttl == expected_ttl
        stored = json.loads(value)
        assert stored["user_id"] == 7
        assert stored["agent_name"] == "my-agent"
        assert stored["system_prompt"] == "Be helpful."

    def test_create_session_redis_failure_raises(self, svc):
        """Redis write failure raises RuntimeError; session must not remain in memory."""
        redis_mock = AsyncMock()
        redis_mock.setex = AsyncMock(side_effect=Exception("Redis down"))
        svc._get_redis = AsyncMock(return_value=redis_mock)

        with pytest.raises(RuntimeError, match="Failed to persist"):
            _run(svc.create_session(
                agent_name="agent",
                chat_session_id="cs_1",
                user_id=1,
                user_email="u@example.com",
                system_prompt="prompt",
            ))

        assert len(svc._sessions) == 0
