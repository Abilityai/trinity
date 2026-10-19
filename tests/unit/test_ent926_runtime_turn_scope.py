"""A runtime that cannot scope a shared person's turn does not run one (ent#926).

Claude Code agents carry the turn-tier hook, which holds a shared person's turn
to the paths the Files API allows and gives it no shell. Gemini CLI and Codex
run no Claude Code hooks, so nothing would hold such a turn to anything. Ruling
2026-10-19: those runtimes refuse it, with a named reason, before the CLI starts.

The check sits at runtime spawn because every route a turn takes ends there:
the backend's push to /api/task, the sync /api/chat, and the pull worker
(#3127) that claims a turn the backend never pushes. The backend still decides
the tier (the same `turn-tier` route the hook asks); the agent server enforces.

Module under test: docker/base-image/agent_server/services/turn_scope.py, and
its call at the top of GeminiRuntime / CodexRuntime execute + execute_headless.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import threading
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from agent_server.services import codex_runtime, gemini_runtime, turn_scope

pytestmark = pytest.mark.unit


def _require(monkeypatch, tier, execution_id="exec-1"):
    asked = []

    async def fake_fetch(agent, run):
        asked.append((agent, run))
        return tier
    monkeypatch.setattr(turn_scope, "fetch_tier", fake_fetch)
    monkeypatch.setattr(turn_scope, "_agent_name", lambda: "agent-a")
    asyncio.run(turn_scope.require_owner_tier("Gemini CLI", execution_id))
    return asked


# ---- the decision ---------------------------------------------------------------------

def test_the_owners_turn_runs(monkeypatch):
    assert _require(monkeypatch, "owner") == [("agent-a", "exec-1")]


@pytest.mark.parametrize("tier", ["share", "unknown"])
def test_a_shared_or_unknown_turn_is_refused_with_a_named_reason(monkeypatch, tier):
    with pytest.raises(HTTPException) as exc:
        _require(monkeypatch, tier)
    assert exc.value.status_code == 403
    assert "Gemini CLI" in exc.value.detail and "shared" in exc.value.detail


def test_no_answer_refuses_rather_than_running(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _require(monkeypatch, "unreachable")
    assert exc.value.status_code == 503


def test_a_call_with_no_execution_id_is_no_platform_turn_and_asks_nobody(monkeypatch):
    assert _require(monkeypatch, "share", execution_id=None) == []


# ---- the lookup, over real HTTP ----------------------------------------------------------

class _Platform(http.server.BaseHTTPRequestHandler):
    reply = (200, {"tier": "owner"})
    seen: list = []

    def do_GET(self):  # noqa: N802
        _Platform.seen.append((self.path, self.headers.get("Authorization")))
        status, body = _Platform.reply
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


@pytest.fixture()
def platform(monkeypatch):
    server = http.server.HTTPServer(("127.0.0.1", 0), _Platform)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Platform.seen = []
    monkeypatch.setenv("TRINITY_BACKEND_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "agent-key")
    yield _Platform
    server.shutdown()


def test_the_lookup_sends_the_agents_own_key_to_the_turn_tier_route(platform):
    assert asyncio.run(turn_scope.fetch_tier("agent a", "exec/1")) == "owner"
    assert platform.seen == [("/api/agents/agent%20a/executions/exec%2F1/turn-tier", "Bearer agent-key")]


@pytest.mark.parametrize("status,body,expected", [
    (200, {"tier": "share"}, "share"),
    (200, {"tier": "admin"}, "unreachable"),
    (200, b"not json", "unreachable"),
    (404, {"detail": "nope"}, "unknown"),
    (500, {"detail": "boom"}, "unreachable"),
])
def test_the_lookup_reads_only_a_clean_owner_or_share_answer(platform, status, body, expected):
    platform.reply = (status, body)
    try:
        assert asyncio.run(turn_scope.fetch_tier("agent-a", "exec-1")) == expected
    finally:
        platform.reply = (200, {"tier": "owner"})


def test_no_key_is_no_answer(monkeypatch):
    monkeypatch.delenv("TRINITY_MCP_API_KEY", raising=False)
    assert asyncio.run(turn_scope.fetch_tier("agent-a", "exec-1")) == "unreachable"


def test_an_unreachable_platform_is_no_answer(monkeypatch):
    monkeypatch.setenv("TRINITY_BACKEND_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "agent-key")
    assert asyncio.run(turn_scope.fetch_tier("agent-a", "exec-1")) == "unreachable"


# ---- wired into every Gemini / Codex entry point, before the CLI starts --------------------

def _runtimes(monkeypatch):
    gem = gemini_runtime.GeminiRuntime()
    cod = codex_runtime.CodexRuntime()
    for rt in (gem, cod):
        monkeypatch.setattr(rt, "is_available", lambda: True)

    def no_spawn(*a, **k):
        raise AssertionError("the CLI was started for a refused turn")
    monkeypatch.setattr(gemini_runtime.subprocess, "Popen", no_spawn)
    monkeypatch.setattr(codex_runtime.subprocess, "Popen", no_spawn)
    monkeypatch.setattr(gemini_runtime, "get_process_registry", lambda: MagicMock())
    return gem, cod


@pytest.mark.parametrize("which", ["gemini", "codex"])
@pytest.mark.parametrize("entry", ["execute", "execute_headless"])
def test_a_shared_persons_turn_never_starts_the_cli(monkeypatch, which, entry):
    gem, cod = _runtimes(monkeypatch)
    rt = gem if which == "gemini" else cod

    async def share(agent, run):
        return "share"
    monkeypatch.setattr(turn_scope, "fetch_tier", share)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(getattr(rt, entry)(prompt="hi", execution_id="exec-1"))
    assert exc.value.status_code == 403
