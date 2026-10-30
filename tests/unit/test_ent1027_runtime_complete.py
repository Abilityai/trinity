"""Agent-server one-shot completion on the agent's own runtime
(trinity-enterprise#1027, the in-container half of retrieval-memory extraction).

``POST /api/runtime/complete`` runs ONE prompt through the agent's runtime,
non-interactively, with no tools and no project context, and returns the text —
so the agent's subscription (or its own key) pays for remembering what it did,
never the platform key. The seam is ``AgentRuntime.complete``; a runtime that
has no clean one-shot answers ``unsupported`` honestly.

Module under test: docker/base-image/agent_server/services/runtime_complete.py
and routers/runtime.py. The subprocess is faked at ``Popen``; the registry is
the real class with a fake process (the conftest preloads ``agent_server``).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from agent_server.services import runtime_adapter  # noqa: E402
from agent_server.services import runtime_complete as rc  # noqa: E402
from agent_server.services.process_registry import ProcessRegistry  # noqa: E402
from agent_server.routers import runtime as runtime_router  # noqa: E402
from agent_server.models import RuntimeCompleteRequest  # noqa: E402

pytestmark = pytest.mark.unit

_PGID = 1027


class FakeProcess:
    def __init__(self, stdout="", stderr="", returncode=0, *, timeout=False):
        self.pid = 4242
        self.returncode = returncode
        self.stdin = MagicMock()
        self.stdout = MagicMock()
        self.stderr = MagicMock()
        self._out, self._err, self._timeout = stdout, stderr, timeout
        self.killed = False

    def communicate(self, input=None, timeout=None):  # noqa: A002 — the real signature
        self.sent = input
        if self._timeout:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        return self._out, self._err

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def envelope(result, *, is_error=False):
    return json.dumps({"type": "result", "subtype": "success", "is_error": is_error, "result": result,
                       "duration_ms": 1200, "session_id": "s"})


@pytest.fixture
def spawn(monkeypatch, tmp_path):
    """Fake Popen that records argv/kwargs and asserts the cwd at spawn time."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(rc, "_capture_pgid", lambda process: _PGID)
    killed = []
    monkeypatch.setattr(rc, "_terminate_process_group", lambda process, *a, **k: killed.append((process, k.get("pgid"))))
    registry = ProcessRegistry()
    monkeypatch.setattr(rc, "get_process_registry", lambda: registry)
    calls = []
    holder = {"process": FakeProcess(envelope('["a fact"]'))}

    def fake_popen(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        calls.append({"cmd": cmd, "kwargs": kwargs, "cwd_existed": cwd.is_dir(),
                      "cwd_entries": sorted(p.name for p in cwd.iterdir()) if cwd.is_dir() else None})
        return holder["process"]

    monkeypatch.setattr(rc.subprocess, "Popen", fake_popen)
    return SimpleNamespace(calls=calls, killed=killed, registry=registry, holder=holder)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# The Claude command and spawn
# --------------------------------------------------------------------------- #

class TestClaudeCommand:
    def test_one_turn_no_tools_no_project_context(self, spawn):
        text = run(rc.complete_claude("extract the facts", model="claude-haiku-5-5", timeout_seconds=30, execution_id="m-1"))
        assert text == '["a fact"]'
        call = spawn.calls[0]
        cmd = call["cmd"]
        assert cmd[0] == "claude" and "--print" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "json"
        assert cmd[cmd.index("--max-turns") + 1] == "1"
        assert cmd[cmd.index("--model") + 1] == "claude-haiku-5-5"
        assert cmd[cmd.index("--system-prompt") + 1] == rc.SYSTEM_PROMPT
        assert "--append-system-prompt" not in cmd
        assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers":{}}' and "--strict-mcp-config" in cmd
        denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
        for tool in rc.BUILTIN_TOOLS:
            assert tool in denied
        # the prompt rides on stdin, never argv (MAX_ARG_STRLEN, and no cmdline leak)
        assert "extract the facts" not in " ".join(cmd)
        assert spawn.holder["process"].sent == "extract the facts"

    def test_cwd_is_a_fresh_empty_dir_not_the_workspace_and_is_cleaned(self, spawn, tmp_path):
        run(rc.complete_claude("p", model=None, timeout_seconds=30, execution_id="m-2"))
        call = spawn.calls[0]
        cwd = Path(call["kwargs"]["cwd"])
        assert call["cwd_existed"] and call["cwd_entries"] == []
        assert cwd != rc.WORKSPACE_DIR and not str(cwd).startswith(str(rc.WORKSPACE_DIR) + "/.claude")
        assert str(cwd).startswith(str(tmp_path))
        assert not cwd.exists(), "the scratch dir is removed after the run"
        assert call["kwargs"]["start_new_session"] is True
        env = call["kwargs"]["env"]
        assert env[rc.EXECUTION_TAG_NAME] == "m-2"

    def test_default_model_when_none_given(self, spawn):
        run(rc.complete_claude("p", model=None, timeout_seconds=30, execution_id="m-3"))
        cmd = spawn.calls[0]["cmd"]
        assert cmd[cmd.index("--model") + 1] == rc.DEFAULT_MODEL

    def test_registers_then_unregisters_with_the_real_registry(self, spawn):
        seen = {}
        orig_register = spawn.registry.register

        def spy_register(execution_id, process, metadata=None):
            seen["registered"] = (execution_id, process, metadata)
            seen["running_during"] = execution_id in spawn.registry._processes or True
            return orig_register(execution_id, process, metadata)

        spawn.registry.register = spy_register
        run(rc.complete_claude("p", model=None, timeout_seconds=30, execution_id="m-4"))
        eid, process, metadata = seen["registered"]
        assert eid == "m-4" and process is spawn.holder["process"]
        assert metadata["type"] == "complete" and metadata["pgid"] == _PGID
        assert "m-4" not in spawn.registry._processes, "unregistered in finally"

    def test_result_is_credential_sanitised(self, spawn, monkeypatch):
        from agent_server.utils import credential_sanitizer as cs
        cs.add_credential_value("sk-ant-oat01-SECRETSECRETSECRET")
        spawn.holder["process"] = FakeProcess(envelope('["token sk-ant-oat01-SECRETSECRETSECRET was used"]'))
        text = run(rc.complete_claude("p", model=None, timeout_seconds=30, execution_id="m-5"))
        assert "SECRETSECRET" not in text and "REDACTED" in text


# --------------------------------------------------------------------------- #
# Failure classes
# --------------------------------------------------------------------------- #

class TestFailures:
    def _err(self, spawn, process):
        spawn.holder["process"] = process
        with pytest.raises(rc.CompleteError) as err:
            run(rc.complete_claude("p", model=None, timeout_seconds=30, execution_id="m-e"))
        return err.value

    def test_rate_limit_is_429(self, spawn):
        e = self._err(spawn, FakeProcess("", "Claude AI usage limit reached|1761840000", returncode=1))
        assert (e.status, e.code) == (429, "rate_limited")

    def test_auth_failure_is_502(self, spawn):
        e = self._err(spawn, FakeProcess("", "OAuth token has expired. Please run /login", returncode=1))
        assert (e.status, e.code) == (502, "auth_failure")

    def test_nonzero_exit_is_502_runtime_error(self, spawn):
        e = self._err(spawn, FakeProcess("", "boom", returncode=3))
        assert (e.status, e.code) == (502, "runtime_error")

    def test_empty_output_is_502(self, spawn):
        e = self._err(spawn, FakeProcess("", "", returncode=0))
        assert (e.status, e.code) == (502, "runtime_error")

    def test_error_envelope_is_classified(self, spawn):
        e = self._err(spawn, FakeProcess(envelope("Claude AI usage limit reached", is_error=True), "", returncode=0))
        assert (e.status, e.code) == (429, "rate_limited")

    def test_timeout_is_504_and_the_group_is_killed(self, spawn):
        e = self._err(spawn, FakeProcess(timeout=True))
        assert (e.status, e.code) == (504, "timeout")
        assert spawn.killed and spawn.killed[0][1] == _PGID
        assert "m-e" not in spawn.registry._processes

    def test_non_json_stdout_on_success_is_returned_as_text(self, spawn):
        spawn.holder["process"] = FakeProcess('["plain"]\n', "", returncode=0)
        assert run(rc.complete_claude("p", model=None, timeout_seconds=30, execution_id="m-t")) == '["plain"]'


# --------------------------------------------------------------------------- #
# The seam and the route
# --------------------------------------------------------------------------- #

class TestSeamAndRoute:
    def test_abc_default_is_unsupported(self):
        class Bare(runtime_adapter.AgentRuntime):
            async def execute(self, *a, **k): ...
            def configure_mcp(self, mcp_servers): return True
            def is_available(self): return True
            def get_default_model(self): return "x"
            async def execute_headless(self, *a, **k): ...
        with pytest.raises(runtime_adapter.RuntimeUnsupported):
            run(Bare().complete("p"))

    def _route(self, monkeypatch, runtime, *, available=True, agent_runtime="claude-code"):
        monkeypatch.setattr(runtime_router, "get_runtime", lambda: runtime)
        monkeypatch.setattr(runtime_router.agent_state, "runtime_available", available)
        monkeypatch.setattr(runtime_router.agent_state, "agent_runtime", agent_runtime)

    def test_route_returns_text_runtime_model_and_duration(self, monkeypatch):
        seen = {}

        class RT:
            async def complete(self, prompt, *, model=None, timeout_seconds=60, execution_id=None):
                seen.update(prompt=prompt, model=model, timeout=timeout_seconds, execution_id=execution_id)
                return '["ok"]'
            def get_default_model(self): return "claude-haiku-5-5"
        self._route(monkeypatch, RT())
        out = run(runtime_router.runtime_complete(RuntimeCompleteRequest(prompt="hi", timeout_seconds=30, tag="memory.extract")))
        assert out["text"] == '["ok"]' and out["runtime"] == "claude-code" and out["model"] == "claude-haiku-5-5"
        assert isinstance(out["duration_ms"], int) and out["duration_ms"] >= 0
        assert seen["prompt"] == "hi" and seen["timeout"] == 30 and seen["execution_id"]

    def test_timeout_is_clamped(self, monkeypatch):
        seen = {}

        class RT:
            async def complete(self, prompt, *, model=None, timeout_seconds=60, execution_id=None):
                seen["timeout"] = timeout_seconds
                return "x"
            def get_default_model(self): return "m"
        self._route(monkeypatch, RT())
        run(runtime_router.runtime_complete(RuntimeCompleteRequest(prompt="hi", timeout_seconds=100000)))
        assert seen["timeout"] == 600
        run(runtime_router.runtime_complete(RuntimeCompleteRequest(prompt="hi", timeout_seconds=1)))
        assert seen["timeout"] == 5

    def test_unsupported_runtime_is_503(self, monkeypatch):
        class RT:
            async def complete(self, *a, **k):
                raise runtime_adapter.RuntimeUnsupported("gemini-cli has no one-shot completion")
            def get_default_model(self): return "m"
        self._route(monkeypatch, RT(), agent_runtime="gemini-cli")
        with pytest.raises(HTTPException) as err:
            run(runtime_router.runtime_complete(RuntimeCompleteRequest(prompt="hi")))
        assert err.value.status_code == 503 and err.value.detail["code"] == "unsupported"

    def test_runtime_unavailable_is_503(self, monkeypatch):
        class RT:
            async def complete(self, *a, **k): return "x"
            def get_default_model(self): return "m"
        self._route(monkeypatch, RT(), available=False)
        with pytest.raises(HTTPException) as err:
            run(runtime_router.runtime_complete(RuntimeCompleteRequest(prompt="hi")))
        assert err.value.status_code == 503 and err.value.detail["code"] == "runtime_unavailable"

    def test_complete_error_maps_to_its_status(self, monkeypatch):
        class RT:
            async def complete(self, *a, **k):
                raise rc.CompleteError(429, "rate_limited", "usage limit")
            def get_default_model(self): return "m"
        self._route(monkeypatch, RT())
        with pytest.raises(HTTPException) as err:
            run(runtime_router.runtime_complete(RuntimeCompleteRequest(prompt="hi")))
        assert err.value.status_code == 429 and err.value.detail == {"code": "rate_limited", "message": "usage limit"}

    def test_route_is_registered_on_the_app(self):
        from agent_server.main import app
        assert any(getattr(r, "path", "") == "/api/runtime/complete" and "POST" in getattr(r, "methods", set()) for r in app.routes)

    def test_claude_runtime_wires_the_seam(self, monkeypatch):
        from agent_server.services import claude_code
        seen = {}

        async def fake(prompt, *, model, timeout_seconds, execution_id):
            seen.update(prompt=prompt, model=model, timeout_seconds=timeout_seconds, execution_id=execution_id)
            return "t"
        monkeypatch.setattr(claude_code, "complete_claude", fake)
        out = run(claude_code.ClaudeCodeRuntime().complete("p", model="m", timeout_seconds=12, execution_id="e"))
        assert out == "t" and seen == {"prompt": "p", "model": "m", "timeout_seconds": 12, "execution_id": "e"}

    def test_other_runtimes_answer_unsupported(self):
        from agent_server.services import gemini_runtime, codex_runtime
        for cls in (gemini_runtime.GeminiRuntime, codex_runtime.CodexRuntime):
            with pytest.raises(runtime_adapter.RuntimeUnsupported):
                run(cls().complete("p"))
