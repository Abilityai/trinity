"""Unit tests for services/brain_orb_postprocess.py (trinity-enterprise#102).

The post-voice-session run is dispatched through the STANDARD execution path
(a real, observable execution row + backgrounded execute_task) instead of the
agent hook's detached `claude -p` — which was invisible to the platform and
reaped by the agent-server's cgroup orphan sweep. These tests cover the config
read (fail-closed), the dispatch decision, message composition, and the
fail-soft guarantee (a dispatch problem never raises — the transcript save
must survive it).
"""
from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock, patch

import pytest

import services.brain_orb_postprocess as pp


class _FakeClientCM:
    def __init__(self, *, status=200, content=b"", exc=None):
        self._status = status
        self._content = content
        self._exc = exc

    async def __aenter__(self):
        client = AsyncMock()
        if self._exc is not None:
            client.request = AsyncMock(side_effect=self._exc)
        else:
            client.request = AsyncMock(
                return_value=types.SimpleNamespace(status_code=self._status, content=self._content)
            )
        return client

    async def __aexit__(self, *_a):
        return False


def _fake_httpx(**kw):
    def _factory(*_a, **_k):
        return _FakeClientCM(**kw)
    return _factory


# --- read_postprocess_config -------------------------------------------------

def test_config_read_success():
    with patch.object(pp, "agent_httpx_client",
                      _fake_httpx(content=json.dumps({"enabled": True, "prompt": " sum it "}).encode())):
        cfg = asyncio.run(pp.read_postprocess_config("a"))
    assert cfg == {"enabled": True, "prompt": "sum it"}


def test_config_read_non_200_is_disabled():
    with patch.object(pp, "agent_httpx_client", _fake_httpx(status=404)):
        cfg = asyncio.run(pp.read_postprocess_config("a"))
    assert cfg == {"enabled": False, "prompt": ""}


def test_config_read_transport_error_is_disabled():
    with patch.object(pp, "agent_httpx_client", _fake_httpx(exc=RuntimeError("down"))):
        cfg = asyncio.run(pp.read_postprocess_config("a"))
    assert cfg == {"enabled": False, "prompt": ""}


# --- dispatch_postprocess ----------------------------------------------------

def _enabled_cfg(prompt="Extract decisions."):
    return AsyncMock(return_value={"enabled": True, "prompt": prompt})


def test_dispatch_disabled_reports_reason():
    with patch.object(pp, "read_postprocess_config",
                      AsyncMock(return_value={"enabled": False, "prompt": ""})):
        out = asyncio.run(pp.dispatch_postprocess("a", transcript_path="Brain/x.md"))
    assert out["dispatched"] is False
    assert "disabled" in out["reason"]


def test_dispatch_creates_execution_and_backgrounds_run():
    """The execution row is pre-created (scheduler pattern) so the caller gets a real
    id; execute_task runs in the background against that row."""
    async def _main():
        execute = AsyncMock()
        fake_db = types.SimpleNamespace(
            create_task_execution=lambda **kw: types.SimpleNamespace(id="exec-7", kwargs=kw)
        )
        fake_svc = types.SimpleNamespace(execute_task=execute)
        with patch.object(pp, "read_postprocess_config", _enabled_cfg()), \
             patch("database.db", fake_db), \
             patch("services.task_execution_service.get_task_execution_service",
                   return_value=fake_svc):
            out = await pp.dispatch_postprocess(
                "corny", transcript_path="Brain/00-Inbox/Voice Conversations/v.md",
                source_user_id=7, source_user_email="u@example.com",
            )
            # let the backgrounded task run to completion
            await asyncio.sleep(0)
            for t in list(pp._background_tasks):
                await t
        return out, execute

    out, execute = asyncio.run(_main())
    assert out["dispatched"] is True
    assert out["execution_id"] == "exec-7"
    execute.assert_awaited_once()
    kw = execute.await_args.kwargs
    assert kw["agent_name"] == "corny"
    assert kw["triggered_by"] == "voice"
    assert kw["execution_id"] == "exec-7"
    assert "Extract decisions." in kw["message"]
    assert "Brain/00-Inbox/Voice Conversations/v.md" in kw["message"]


def test_dispatch_failure_is_fail_soft():
    """Any internal error (here: the DB layer exploding) is reported as a reason,
    never raised — the transcript save must survive a postprocess hiccup."""
    async def _main():
        with patch.object(pp, "read_postprocess_config", _enabled_cfg()), \
             patch("database.db",
                   types.SimpleNamespace(create_task_execution=_boom)):
            return await pp.dispatch_postprocess("a", transcript_path="Brain/x.md")

    def _boom(**_kw):
        raise RuntimeError("db down")

    out = asyncio.run(_main())
    assert out["dispatched"] is False
    assert "dispatch failed" in out["reason"]


def test_dispatch_execute_task_error_contained():
    """A failing execute_task never propagates out of the background task (the row's
    terminal status is the surface)."""
    async def _main():
        execute = AsyncMock(side_effect=RuntimeError("agent 503"))
        fake_db = types.SimpleNamespace(
            create_task_execution=lambda **kw: types.SimpleNamespace(id="exec-8")
        )
        fake_svc = types.SimpleNamespace(execute_task=execute)
        with patch.object(pp, "read_postprocess_config", _enabled_cfg()), \
             patch("database.db", fake_db), \
             patch("services.task_execution_service.get_task_execution_service",
                   return_value=fake_svc):
            out = await pp.dispatch_postprocess("a", transcript_path="Brain/x.md")
            await asyncio.sleep(0)
            for t in list(pp._background_tasks):
                await t   # must not raise
        return out

    out = asyncio.run(_main())
    assert out["dispatched"] is True   # dispatch succeeded; the FAILURE lives on the row


# --- message composition -----------------------------------------------------

def test_compose_message_with_path_names_output_note():
    msg = pp._compose_message("Do the thing.",
                              transcript_path="Brain/V/Voice - 2026-07-06 1408.md",
                              transcript_text=None)
    assert "Brain/V/Voice - 2026-07-06 1408.md" in msg
    assert "Voice - 2026-07-06 1408 - processed.md" in msg
    assert "Do the thing." in msg
    assert msg.startswith("[Trinity Brain Orb: post-voice-session processing]")


def test_compose_message_with_inline_text():
    msg = pp._compose_message("Summarize.", transcript_path=None,
                              transcript_text="You: hi\nAgent: hello")
    assert "## Transcript" in msg
    assert "You: hi" in msg
    assert "Summarize." in msg
