"""Unit tests for the agent-side fire-and-forget result callback (#1083, PR2.D).

Covers the pure logic (no container): runtime/eligibility gating, the typed
envelope mapping, disk persist/resend, and the retry-to-deadline delivery loop.
The conftest preloads the agent_server namespace package.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from agent_server.services import result_callback as rc  # noqa: E402

pytestmark = pytest.mark.unit


def _req(**over):
    base = dict(
        message="hi", model="sonnet", allowed_tools=None, system_prompt=None,
        timeout_seconds=300, max_turns=None, execution_id="exec-1",
        resume_session_id=None, persist_session=False, images=None,
        async_result=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        if isinstance(r, Exception):
            raise r
        return _FakeResp(r)


# ---------------------------------------------------------------------------
# Envelope mapping
# ---------------------------------------------------------------------------
class TestEnvelopeMapping:
    def test_success_envelope(self):
        md = SimpleNamespace(model_dump=lambda: {"cost_usd": 0.1})
        env = rc._success_envelope("resp", [{"t": 1}], md, "sess-1")
        assert env["status"] == "success"
        assert env["terminal_reason"] == "completed"
        assert env["metadata"] == {"cost_usd": 0.1}
        assert env["session_id"] == "sess-1"

    def test_auth_503_maps_to_auth(self):
        env = rc._envelope_from_http_exception(HTTPException(status_code=503, detail="Authentication failure: bad"))
        assert env["status"] == "failed"
        assert env["error_code"] == "auth"
        assert env["terminal_reason"] == "auth"
        assert "Authentication" in env["error"]

    def test_timeout_504_maps_to_timeout(self):
        env = rc._envelope_from_http_exception(HTTPException(status_code=504, detail="timed out"))
        assert env["error_code"] == "timeout"
        assert env["terminal_reason"] == "max_duration"

    def test_empty_result_502_dict_carries_metadata(self):
        detail = {"message": "no result", "metadata": {"cost_usd": 0.02, "num_turns": 1}}
        env = rc._envelope_from_http_exception(HTTPException(status_code=502, detail=detail))
        assert env["error_code"] is None
        assert env["terminal_reason"] == "empty_result"
        assert env["metadata"] == {"cost_usd": 0.02, "num_turns": 1}
        assert env["error"] == "no result"

    def test_unknown_status_falls_back_to_error(self):
        env = rc._envelope_from_http_exception(HTTPException(status_code=500, detail="boom"))
        assert env["error_code"] is None
        assert env["terminal_reason"] == "error"


# ---------------------------------------------------------------------------
# Eligibility gating
# ---------------------------------------------------------------------------
class TestTrySpawnGating:
    def test_not_async_returns_false(self, monkeypatch):
        monkeypatch.setattr(rc.agent_state, "agent_runtime", "claude-code")
        assert rc.try_spawn_async(_req(async_result=False)) is False

    def test_non_claude_runtime_returns_false(self, monkeypatch):
        monkeypatch.setattr(rc.agent_state, "agent_runtime", "gemini-cli")
        monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
        monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_k")
        assert rc.try_spawn_async(_req()) is False

    def test_missing_execution_id_returns_false(self, monkeypatch):
        monkeypatch.setattr(rc.agent_state, "agent_runtime", "claude-code")
        monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
        monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_k")
        assert rc.try_spawn_async(_req(execution_id=None)) is False

    def test_missing_creds_returns_false(self, monkeypatch):
        monkeypatch.setattr(rc.agent_state, "agent_runtime", "claude-code")
        monkeypatch.delenv("TRINITY_BACKEND_URL", raising=False)
        monkeypatch.delenv("TRINITY_MCP_API_KEY", raising=False)
        assert rc.try_spawn_async(_req()) is False

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../../etc/passwd",          # path traversal
            "a/b",                       # path separator
            "a\\b",                      # windows separator
            "..",                        # parent dir
            "id.with.dots",              # '.' not in token_urlsafe / UUID charset
            "id with space",
            "x" * 129,                   # over the length cap
            "",                          # empty (caught earlier, but covered)
        ],
    )
    def test_unsafe_execution_id_returns_false(self, monkeypatch, bad_id):
        # Defense-in-depth (#1083): a non-token/UUID execution_id must never reach
        # the pending-results path build / callback URL — fall back to sync.
        monkeypatch.setattr(rc.agent_state, "agent_runtime", "claude-code")
        monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
        monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_k")
        assert rc.try_spawn_async(_req(execution_id=bad_id)) is False

    def test_is_safe_execution_id_accepts_real_ids(self):
        # secrets.token_urlsafe(16) charset + UUID forms are accepted.
        assert rc._is_safe_execution_id("Ab3_-xYz09kLmNoPqRsTuv") is True
        assert rc._is_safe_execution_id("550e8400-e29b-41d4-a716-446655440000") is True
        assert rc._is_safe_execution_id("../escape") is False
        assert rc._is_safe_execution_id(None) is False

    def test_eligible_spawns_detached_task(self, monkeypatch):
        monkeypatch.setattr(rc.agent_state, "agent_runtime", "claude-code")
        monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
        monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_k")

        async def _go():
            with patch.object(rc, "_run_and_report", AsyncMock()) as m:
                ok = rc.try_spawn_async(_req())
                await asyncio.sleep(0)  # let the detached task run
                await asyncio.sleep(0)
                return ok, m

        ok, m = asyncio.run(_go())
        assert ok is True
        m.assert_awaited_once()


# ---------------------------------------------------------------------------
# Persist / resend roundtrip
# ---------------------------------------------------------------------------
class TestPersistResend:
    def test_persist_and_delete(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "_PENDING_DIR", tmp_path)
        rc._persist("exec-9", {"agent_name": "a", "envelope": {"status": "success"}})
        assert (tmp_path / "exec-9.json").exists()
        rc._delete("exec-9")
        assert not (tmp_path / "exec-9.json").exists()

    def test_persist_delete_reject_traversal(self, tmp_path, monkeypatch):
        # #950 containment (normpath + startswith), inlined co-located with the
        # write/unlink sinks: a traversal id is rejected best-effort — nothing is
        # written outside _PENDING_DIR and neither call raises.
        monkeypatch.setattr(rc, "_PENDING_DIR", tmp_path)
        for bad in ("../escape", "../../etc/passwd"):
            rc._persist(bad, {"agent_name": "a", "envelope": {}})  # must not raise
            rc._delete(bad)  # must not raise
        assert not (tmp_path.parent / "escape.json").exists()
        assert not (tmp_path / "escape.json").exists()

    def test_resend_delivers_and_deletes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "_PENDING_DIR", tmp_path)
        monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
        monkeypatch.setenv("TRINITY_MCP_API_KEY", "k")
        monkeypatch.setattr(rc.agent_state, "agent_name", "agent-a")
        (tmp_path / "exec-r.json").write_text(
            json.dumps({"agent_name": "agent-a", "envelope": {"status": "success"}})
        )
        with patch.object(rc.httpx, "AsyncClient", lambda **kw: _FakeClient([200])):
            asyncio.run(rc.resend_pending_results())
        assert not (tmp_path / "exec-r.json").exists()  # delivered → deleted

    def test_resend_drops_corrupt_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "_PENDING_DIR", tmp_path)
        monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
        monkeypatch.setenv("TRINITY_MCP_API_KEY", "k")
        (tmp_path / "exec-bad.json").write_text("{not json")
        with patch.object(rc.httpx, "AsyncClient", lambda **kw: _FakeClient([200])):
            asyncio.run(rc.resend_pending_results())
        assert not (tmp_path / "exec-bad.json").exists()


# ---------------------------------------------------------------------------
# Delivery retry loop
# ---------------------------------------------------------------------------
class TestDeliver:
    def _deliver(self, responses, deadline_offset=1000.0):
        import time

        deadline = time.monotonic() + deadline_offset
        with (
            patch.object(rc.httpx, "AsyncClient", lambda **kw: _FakeClient(responses)),
            patch.object(rc.asyncio, "sleep", AsyncMock()),
        ):
            return asyncio.run(
                rc._deliver("exec-1", "agent-a", {"status": "success"}, "http://b", "k", deadline)
            )

    def test_2xx_delivers(self):
        assert self._deliver([200]) is True

    def test_permanent_4xx_stops_without_retry(self):
        for code in (403, 404, 409, 413):
            client_holder = _FakeClient([code])
            with (
                patch.object(rc.httpx, "AsyncClient", lambda **kw: client_holder),
                patch.object(rc.asyncio, "sleep", AsyncMock()),
            ):
                import time
                ok = asyncio.run(
                    rc._deliver("e", "a", {}, "http://b", "k", time.monotonic() + 1000)
                )
            assert ok is True
            assert client_holder.calls == 1, f"{code} must not be retried"

    def test_transient_5xx_retries_then_succeeds(self):
        assert self._deliver([503, 503, 200]) is True

    def test_deadline_returns_false(self):
        # deadline already passed → first non-2xx returns False (no delivery).
        assert self._deliver([500], deadline_offset=-1.0) is False
