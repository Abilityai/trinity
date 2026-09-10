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

    # ent#534 — session-lifetime config the real SDK carries; the service reads
    # them through getattr so their absence is also a supported shape.
    class _ContextWindowCompressionConfig:
        def __init__(self, **kw): self.__dict__.update(kw)

    class _SlidingWindow:
        def __init__(self, **kw): self.__dict__.update(kw)

    class _SessionResumptionConfig:
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
    gtypes.ContextWindowCompressionConfig = _ContextWindowCompressionConfig
    gtypes.SlidingWindow = _SlidingWindow
    gtypes.SessionResumptionConfig = _SessionResumptionConfig

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
    config_mod.WORKSPACE_VOICE_MAX_DURATION = 1800  # ent#534
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

# Snapshot real modules before installing the stubs that gemini_voice's
# import chain needs. After gemini_voice has imported, restore the originals
# so we don't pollute later unit files. Examples of breakage we'd otherwise
# leak:
#   * `config` stub omits EMAIL_PROVIDER → test_file_upload's
#     telegram_adapter → services.email_service import fails.
#   * services.docker_service / services.template_service stubs replace real
#     coroutines with plain Mocks → workspace-delivery awaits explode.
_voice_tools_pre_stub = {
    name: sys.modules.get(name)
    for name in (
        "config",
        "services.docker_service",
        "services.template_service",
    )
}
_stub_config()
_stub_services_package()

# Evict any stale stub registered by test_voice_auth.py when both files run in
# the same pytest session — that module installs a fake services.gemini_voice
# for isolation, and without this eviction the real class is unreachable.
sys.modules.pop("services.gemini_voice", None)

# Now we can import the service
from services.gemini_voice import (  # noqa: E402
    GeminiVoiceService, VoiceSession, _TOOL_PROMPT_MAX,
    _PANEL_TOOL_NAMES, _classify_image_src, _WORKSPACE_ROOT,
)

# gemini_voice has captured what it needed from the docker/template stubs.
# Restore the real modules so other unit files (e.g. test_file_upload) see
# the real services.docker_service whose methods are real coroutines.
for _name, _orig in _voice_tools_pre_stub.items():
    if _orig is not None:
        sys.modules[_name] = _orig
    else:
        sys.modules.pop(_name, None)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_session(agent_name="test-agent", workspace_mode=False) -> VoiceSession:
    # ent#535: `workspace_mode` now decides whether the canvas tools are in the
    # session's manifest at all, and the dispatcher refuses anything outside it.
    # A test that drives a PANEL tool through `_execute_and_respond` therefore
    # has to be a workspace session — which is the only kind that could ever
    # have been offered one (`_build_live_config` has always gated them the same
    # way); a panel call on a phone session was never reachable from the model.
    return VoiceSession(
        session_id="vs_test",
        agent_name=agent_name,
        chat_session_id="cs_test",
        user_id=1,
        user_email="user@example.com",
        system_prompt="You are a test agent.",
        workspace_mode=workspace_mode,
    )


@pytest.fixture
def svc():
    return GeminiVoiceService()


# ── Tests: _execute_tool ──────────────────────────────────────────────────────

class TestExecuteTool:

    @pytest.fixture(autouse=True)
    def _restore_agent_client(self):
        """Snapshot and restore sys.modules['services.agent_client'] per test.

        Each test below installs an *incomplete* `services.agent_client` stub
        (e.g. missing the `AgentClient` class). Without teardown, the next
        unit-suite file that does `from services.agent_client import AgentClient`
        (sync_health_service, fleet status helpers, etc.) imports the polluted
        stub and fails. Snapshotting per test keeps the contamination scoped
        to the body of each individual test.
        """
        sentinel = object()
        original = sys.modules.get("services.agent_client", sentinel)
        try:
            yield
        finally:
            if original is sentinel:
                sys.modules.pop("services.agent_client", None)
            else:
                sys.modules["services.agent_client"] = original

    def test_success(self, svc):
        """Patching _execute_tool itself returns the expected value."""
        with patch.object(svc, "_execute_tool", AsyncMock(return_value="The answer is 42.")):
            result = _run(svc._execute_tool("test-agent", "run_task", {"prompt": "What is 42?"}))
        assert result == "The answer is 42."

    def test_success_real(self, svc):
        """Test _execute_tool with a mocked get_agent_client."""
        mock_response = MagicMock()
        # AgentChatResponse exposes `.response_text`, not `.response` — guards the
        # #979 regression where _execute_tool read the wrong attribute.
        mock_response.response_text = "Task result text."
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
        mock_response.response_text = "ok"
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
        session = _make_session(workspace_mode=True)
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
        session = _make_session(workspace_mode=True)
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
        session = _make_session(workspace_mode=True)
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


# ── Tests: _execute_panel_tool (ent#536 — the panel IS the default canvas) ───

class _CanvasHarness:
    """Fake the canvas read + the one write path, so the tests see exactly what
    the voice verbs would store — and nothing touches a database."""

    def __init__(self, monkeypatch, current=None):
        from database import db
        from services import canvas_service

        self.current = current
        self.writes = []
        monkeypatch.setattr(db, "get_agent_canvas",
                            lambda agent, canvas_id, audience=None: self.current)

        def _write(agent, canvas_id, blocks, *, title, audience, execution_id, template=None):
            self.writes.append({"agent": agent, "canvas_id": canvas_id, "blocks": blocks,
                                "title": title, "audience": audience,
                                "execution_id": execution_id, "template": template})
            return {"blocks": blocks}

        monkeypatch.setattr(canvas_service, "write_canvas", _write)

    @property
    def last(self):
        return self.writes[-1]


def _existing(*blocks, audience="operator", title="Board"):
    return {"agent_name": "test-agent", "canvas_id": "main", "title": title,
            "audience": audience, "blocks": list(blocks)}


class TestExecutePanelTool:
    """Panel verbs are block edits on the agent's default canvas, written
    through the same path as set_canvas — never a separate panel_state."""

    def test_show_markdown_writes_a_voice_block_on_main(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch)
        result = svc._execute_panel_tool(_make_session(), "show_markdown", {"content": "# Hello"})
        assert result == "Panel updated."
        assert h.last["canvas_id"] == "main"
        assert h.last["blocks"] == [
            {"id": "voice", "kind": "markdown", "title": None, "payload": {"markdown": "# Hello"}}
        ]

    def test_title_is_the_block_title_never_the_canvas_title(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch, _existing(title="Agent's board"))
        svc._execute_panel_tool(_make_session(), "show_markdown", {"content": "body", "title": "My Title"})
        assert h.last["title"] == "Agent's board"
        assert h.last["blocks"][-1]["title"] == "My Title"

    def test_blocks_the_agent_wrote_with_set_canvas_survive_a_call(self, svc, monkeypatch):
        board = {"id": "b1", "kind": "kpi", "payload": {"tiles": []}}
        h = _CanvasHarness(monkeypatch, _existing(board, {"id": "voice", "kind": "html", "payload": {"html": "old"}}))
        svc._execute_panel_tool(_make_session(), "show_diagram", {"diagram": "graph TD; A-->B", "title": "Flow"})
        assert h.last["blocks"][0] == board
        assert h.last["blocks"][1] == {"id": "voice", "kind": "diagram", "title": "Flow",
                                       "payload": {"mermaid": "graph TD; A-->B"}}

    def test_update_panel_is_an_html_block(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch)
        svc._execute_panel_tool(_make_session(), "update_panel", {"html": "<b>bold</b>", "title": "Report"})
        assert h.last["blocks"] == [{"id": "voice", "kind": "html", "title": "Report", "payload": {"html": "<b>bold</b>"}}]

    def test_append_to_panel_grows_the_trailing_voice_html_block(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch, _existing({"id": "voice", "kind": "html", "title": None, "payload": {"html": "A"}}))
        svc._execute_panel_tool(_make_session(), "append_to_panel", {"html": "B"})
        assert h.last["blocks"] == [{"id": "voice", "kind": "html", "title": None, "payload": {"html": "AB"}}]

    def test_append_after_a_markdown_panel_starts_a_new_voice_html_block(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch, _existing({"id": "voice", "kind": "markdown", "payload": {"markdown": "m"}}))
        svc._execute_panel_tool(_make_session(), "append_to_panel", {"html": "B"})
        assert [b["id"] for b in h.last["blocks"]] == ["voice", "voice-2"]
        assert h.last["blocks"][1]["kind"] == "html"

    def test_clear_panel_removes_only_the_voice_blocks(self, svc, monkeypatch):
        board = {"id": "b1", "kind": "kpi", "payload": {"tiles": []}}
        h = _CanvasHarness(monkeypatch, _existing({"id": "voice", "kind": "html", "payload": {"html": "x"}},
                                                  board,
                                                  {"id": "voice-2", "kind": "html", "payload": {"html": "y"}}))
        result = svc._execute_panel_tool(_make_session(), "clear_panel", {})
        assert result == "Panel cleared."
        assert h.last["blocks"] == [board]

    def test_a_write_onto_a_wider_audience_canvas_is_refused(self, svc, monkeypatch):
        """An operator call must never land on a customer-visible board."""
        h = _CanvasHarness(monkeypatch, _existing(audience="roster"))
        result = svc._execute_panel_tool(_make_session(), "show_markdown", {"content": "private"})
        assert "not updated" in result and "roster" in result
        assert h.writes == []

    def test_a_roster_session_may_write_a_roster_canvas_and_keeps_the_stored_audience(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch, _existing(audience="roster"))
        session = _make_session(workspace_mode=True)
        session.canvas_audience = "roster"
        svc._execute_panel_tool(session, "show_markdown", {"content": "shared"})
        assert h.last["audience"] == "roster"

    def test_a_roster_session_never_widens_an_operator_canvas(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch, _existing(audience="operator"))
        session = _make_session(workspace_mode=True)
        session.canvas_audience = "roster"
        svc._execute_panel_tool(session, "show_markdown", {"content": "x"})
        assert h.last["audience"] == "operator"

    def test_a_new_canvas_takes_the_session_audience(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch, None)
        svc._execute_panel_tool(_make_session(), "show_markdown", {"content": "x"})
        assert h.last["audience"] == "operator"
        assert h.last["title"] is None
        assert h.last["execution_id"] is None

    def test_a_canvas_write_failure_never_raises_into_the_voice_turn(self, svc, monkeypatch):
        from services import canvas_service
        _CanvasHarness(monkeypatch)

        def _boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(canvas_service, "write_canvas", _boom)
        result = svc._execute_panel_tool(_make_session(), "show_markdown", {"content": "x"})
        assert "could not be saved" in result

    def test_panel_tool_routed_not_forwarded_to_agent(self, svc, monkeypatch):
        """Panel tools must not reach _execute_tool (no agent container call)."""
        h = _CanvasHarness(monkeypatch)
        session = _make_session(workspace_mode=True)
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

        assert h.last["blocks"][0]["kind"] == "markdown"
        gemini_session.send_tool_response.assert_awaited_once()

    # ── #979: show_diagram (Mermaid) ─────────────────────────────────────────

    def test_show_diagram_is_a_diagram_block(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch)
        result = svc._execute_panel_tool(
            _make_session(), "show_diagram", {"diagram": "graph TD; A-->B", "title": "Flow"}
        )
        assert result == "Panel updated."
        assert h.last["blocks"] == [{"id": "voice", "kind": "diagram", "title": "Flow",
                                     "payload": {"mermaid": "graph TD; A-->B"}}]

    def test_show_diagram_missing_arg_is_refused_and_writes_nothing(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch)
        result = svc._execute_panel_tool(_make_session(), "show_diagram", {})
        assert "No diagram source" in result
        assert h.writes == []

    # ── #979: show_image (web URL + workspace path) ──────────────────────────

    def test_show_image_web_url(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch)
        result = svc._execute_panel_tool(
            _make_session(), "show_image",
            {"src": "https://example.com/chart.png", "caption": "A chart"},
        )
        assert result == "Panel updated."
        assert h.last["blocks"] == [{"id": "voice", "kind": "image", "title": None, "payload": {
            "src": "https://example.com/chart.png", "src_kind": "url", "caption": "A chart"}}]

    def test_show_image_relative_workspace_path_normalized(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch)
        svc._execute_panel_tool(_make_session(), "show_image", {"src": "content/chart.png"})
        payload = h.last["blocks"][0]["payload"]
        assert payload["src_kind"] == "path"
        assert payload["src"] == f"{_WORKSPACE_ROOT}/content/chart.png"

    def test_show_image_absolute_workspace_path(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch)
        svc._execute_panel_tool(_make_session(), "show_image", {"src": "/home/developer/content/a.png"})
        assert h.last["blocks"][0]["payload"]["src"] == "/home/developer/content/a.png"

    def test_show_image_tilde_is_workspace_relative(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch)
        svc._execute_panel_tool(_make_session(), "show_image", {"src": "~/content/a.png"})
        assert h.last["blocks"][0]["payload"]["src"] == f"{_WORKSPACE_ROOT}/content/a.png"

    def test_show_image_inline_data_uri_under_the_cap_is_allowed(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch)
        svc._execute_panel_tool(_make_session(), "show_image", {"src": "data:image/png;base64,AAAA"})
        assert h.last["blocks"][0]["payload"]["src_kind"] == "data"

    def test_show_image_empty_src_rejected(self, svc, monkeypatch):
        h = _CanvasHarness(monkeypatch)
        result = svc._execute_panel_tool(_make_session(), "show_image", {"src": "   "})
        assert "No image source" in result
        assert h.writes == []

    @pytest.mark.parametrize("bad_src", [
        "../../etc/passwd",                       # relative traversal escapes root
        "/home/developer/../etc/passwd",          # absolute traversal escapes root
        "/etc/passwd",                            # outside the workspace entirely
        "/home/developer-evil/secret",            # sibling-prefix escape (the startswith bug)
        "data:image/svg+xml;base64,AAAA",         # an SVG is a document, not a raster
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",                     # non-http scheme not allowed
        "ftp://host/x.png",                        # non-http scheme not allowed
        "//evil.example/x.png",                   # protocol-relative
    ])
    def test_show_image_rejects_unsafe_src(self, svc, monkeypatch, bad_src):
        h = _CanvasHarness(monkeypatch)
        result = svc._execute_panel_tool(_make_session(), "show_image", {"src": bad_src})
        assert "rejected" in result.lower()
        # Nothing is written for a rejected source.
        assert h.writes == []


# ── #979: _classify_image_src path-confinement unit tests ────────────────────

class TestClassifyImageSrc:
    """Direct tests of the show_image src classifier / confinement gate."""

    @pytest.mark.parametrize("url", [
        "https://example.com/a.png",
        "HTTPS://EXAMPLE.COM/A.PNG",  # scheme match is case-insensitive
    ])
    def test_web_urls_classified_as_url(self, url):
        out = _classify_image_src(url)
        assert out is not None and out[1] == "url"
        assert out[0] == url  # value preserved verbatim

    def test_relative_path_resolved_under_root(self):
        out = _classify_image_src("content/x.png")
        assert out == (f"{_WORKSPACE_ROOT}/content/x.png", "path")

    def test_root_itself_allowed(self):
        out = _classify_image_src("/home/developer")
        assert out == (_WORKSPACE_ROOT, "path")

    @pytest.mark.parametrize("bad", [
        "",
        "../escape.png",
        "/home/developer/../../etc/passwd",
        "/etc/passwd",
        "/home/developer-evil/x.png",  # the sibling-prefix bug this gate fixes
        "data:text/html,<script>alert(1)</script>",
        "data:image/svg+xml;base64,AAAA",
        "http://example.com/a.png",    # plaintext — refused at write, not left to the CSP (ent#536)
        "file:///etc/passwd",
        "javascript:alert(1)",         # has no scheme://, treated as path, escapes → rejected
    ])
    def test_unsafe_inputs_rejected(self, bad):
        assert _classify_image_src(bad) is None

    def test_inline_raster_under_the_cap_is_data(self):
        assert _classify_image_src("data:image/png;base64,AAAA") == ("data:image/png;base64,AAAA", "data")


# ── #979: new panel tools are registered + declared ──────────────────────────

class TestNewPanelToolRegistration:

    def test_new_tools_in_panel_tool_names(self):
        assert "show_diagram" in _PANEL_TOOL_NAMES
        assert "show_image" in _PANEL_TOOL_NAMES

    def test_new_tools_declared(self):
        from services.gemini_voice import _PANEL_TOOLS
        names = {fd.name for fd in _PANEL_TOOLS.function_declarations}
        assert "show_diagram" in names
        assert "show_image" in names

    def test_show_diagram_requires_diagram_arg(self):
        from services.gemini_voice import _PANEL_TOOLS
        fd = next(f for f in _PANEL_TOOLS.function_declarations if f.name == "show_diagram")
        assert "diagram" in (fd.parameters.required or [])

    def test_show_image_requires_src_arg(self):
        from services.gemini_voice import _PANEL_TOOLS
        fd = next(f for f in _PANEL_TOOLS.function_declarations if f.name == "show_image")
        assert "src" in (fd.parameters.required or [])

    def test_show_diagram_routed_not_forwarded_to_agent(self, svc, monkeypatch):
        """show_diagram executes in-process, never reaching the agent container."""
        h = _CanvasHarness(monkeypatch)
        session = _make_session(workspace_mode=True)
        session._active = True
        gemini_session = MagicMock()
        gemini_session.send_tool_response = AsyncMock()
        session._gemini_session = gemini_session
        session._on_tool_call = None
        session._on_tool_result = None

        fc = MagicMock()
        fc.id = "fc_diag"
        fc.name = "show_diagram"
        fc.args = {"diagram": "graph TD; A-->B"}

        with patch.object(svc, "_execute_tool", AsyncMock()) as mock_exec:
            _run(svc._execute_and_respond(session, "fc_diag", fc))
            mock_exec.assert_not_awaited()

        assert h.last["blocks"][0]["kind"] == "diagram"
        gemini_session.send_tool_response.assert_awaited_once()


# ── Tests: end_session cancels pending tool tasks ─────────────────────────────

class TestEndSession:

    def test_cancels_pending_tool_tasks(self, svc):
        session = _make_session(workspace_mode=True)
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
        session = _make_session(workspace_mode=True)
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
        session = _make_session(workspace_mode=True)
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

    def test_the_redis_blob_carries_every_decided_field(self, svc):
        """ROUND-TRIP COMPLETENESS, not three named keys.

        This test asserted `user_id`, `agent_name` and `system_prompt` and
        nothing else, so ent#535 could add a decision field, omit it from the
        blob, and stay green — which is exactly what happened to
        `tool_manifest`: the cross-worker rebuild read it as "never resolved"
        and handed the model the full platform default, silently unlocking a
        manifest the agent had narrowed.

        The rule the block comment above the dict states is "EVERY field a
        reconstructed session decides on must be here". Asserted here as a
        SUBSET relation over the session's own fields, so a field added
        tomorrow fails this rather than shipping unpersisted. Fields that are
        deliberately not persisted are listed with the reason.
        """
        import dataclasses

        redis_mock = self._make_redis_mock()
        svc._get_redis = AsyncMock(return_value=redis_mock)
        session = _run(svc.create_session(
            agent_name="my-agent", chat_session_id="cs_1", user_id=7,
            user_email="test@example.com", system_prompt="Be helpful.",
        ))
        stored = json.loads(redis_mock.setex.call_args[0][2])

        # Runtime state, not decisions: live objects, per-connection tasks, and
        # values a rebuilt session legitimately starts fresh from.
        not_persisted = {
            "transcript",        # rebuilt worker records its own leg
            "end_reason",        # set by whichever worker ends the call
            "end_message",
        }
        decided = {
            f.name for f in dataclasses.fields(session)
            if not f.name.startswith("_")
        } - not_persisted
        missing = sorted(decided - set(stored))
        assert not missing, (
            "these session fields are decided at start and are NOT in the Redis "
            f"blob, so a cross-worker rebuild silently defaults them: {missing}"
        )

    def test_a_narrowed_tool_manifest_survives_the_rebuild(self, svc):
        """The critical, end to end: worker A resolves, worker B reconstructs.

        Production runs `--workers 2` and the WebSocket routinely lands on a
        different worker than `/voice/start`, so this IS the normal path, not an
        edge case.
        """
        from services.gemini_voice import _session_manifest

        redis_mock = self._make_redis_mock()
        svc._get_redis = AsyncMock(return_value=redis_mock)
        session = _run(svc.create_session(
            agent_name="my-agent", chat_session_id="cs_1", user_id=7,
            user_email="test@example.com", system_prompt="p",
            workspace_mode=True, declared_tools=["run_task"],
        ))
        assert _session_manifest(session) == frozenset({"run_task"})

        raw = redis_mock.setex.call_args[0][2]
        redis_mock.get = AsyncMock(return_value=raw)
        svc._sessions.clear()                       # worker B has never seen it
        rebuilt = _run(svc.get_session(session.session_id))

        assert rebuilt is not None
        assert _session_manifest(rebuilt) == frozenset({"run_task"}), (
            "the rebuilt session was handed the full platform default — the "
            "'locked' manifest is unlocked by the cross-worker rebuild"
        )

    def test_an_empty_manifest_survives_as_a_decision(self, svc):
        """`None` != `[]` across the round trip.

        An agent declaring NO tools is the strongest possible narrowing, and it
        is the one a falsy read turns into the widest possible manifest — the
        exact inversion ent#535's `_session_manifest` docstring records having
        already made once.
        """
        from services.gemini_voice import _session_manifest, _manifest_from_meta

        redis_mock = self._make_redis_mock()
        svc._get_redis = AsyncMock(return_value=redis_mock)
        session = _run(svc.create_session(
            agent_name="my-agent", chat_session_id="cs_1", user_id=7,
            user_email="test@example.com", system_prompt="p",
            workspace_mode=True, declared_tools=[],
        ))
        stored = json.loads(redis_mock.setex.call_args[0][2])
        assert stored["tool_manifest"] == []          # a decision, serialized
        assert _manifest_from_meta(stored) == frozenset()

        redis_mock.get = AsyncMock(return_value=redis_mock.setex.call_args[0][2])
        svc._sessions.clear()
        rebuilt = _run(svc.get_session(session.session_id))
        assert _session_manifest(rebuilt) == frozenset()

    def test_an_older_blob_with_no_manifest_falls_back_to_the_default(self, svc):
        """A session written by a worker that predates this field, read by one
        that does not — the mid-deploy case. Absent means "never resolved",
        which is the pre-ent#535 surface and the safe answer."""
        from services.gemini_voice import _session_manifest, platform_default_tools

        redis_mock = self._make_redis_mock()
        svc._get_redis = AsyncMock(return_value=redis_mock)
        session = _run(svc.create_session(
            agent_name="my-agent", chat_session_id="cs_1", user_id=7,
            user_email="test@example.com", system_prompt="p", workspace_mode=True,
        ))
        stored = json.loads(redis_mock.setex.call_args[0][2])
        stored.pop("tool_manifest", None)
        redis_mock.get = AsyncMock(return_value=json.dumps(stored))
        svc._sessions.clear()

        rebuilt = _run(svc.get_session(session.session_id))
        assert _session_manifest(rebuilt) == platform_default_tools(workspace_mode=True)

    def test_a_corrupt_manifest_reads_as_unresolved_not_as_a_crash(self):
        """The blob is JSON someone could hand-edit; a dict or a string is not a
        manifest and must not reach the audio loop as one."""
        from services.gemini_voice import _manifest_from_meta

        for bad in ({"tool_manifest": {"a": 1}}, {"tool_manifest": "run_task"},
                    {"tool_manifest": 7}, {"tool_manifest": None}, {}):
            assert _manifest_from_meta(bad) is None

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
