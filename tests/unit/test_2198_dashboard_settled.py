"""#2198 — the dashboard probe must say whether its answer is FINAL.

Why this exists
---------------
`GET /api/agent-dashboard/{name}` answered `{"has_dashboard": false,
"error": "<string>"}` in two completely different situations:

  1. the agent ran its handler and reported "there is no dashboard.yaml"
     (agent_server/routers/dashboard.py returns exactly that shape), and
  2. the agent could not be reached at all — timeout, connection error, non-200.

They were byte-indistinguishable, so the only safe frontend behaviour was to
assume (2) and retry. `AgentDetail.checkDashboardExists()` therefore re-probed
at 0s / 3s / 9s on EVERY page load of an agent that will never have a
dashboard — 3 requests and ~9 seconds, forever, and #2130 recorded that same
ladder as what delayed deep-link landing by ~10s.

`settled` carries the missing bit. It is derived from the TRANSPORT (an HTTP
200 with a parseable body), never from the error text, so it is correct on every
already-deployed agent image and needs no base-image rebuild.

The load-bearing assertion is not "settled is True when the agent answers" — it
is that `settled` is ABSENT on every inconclusive path, because a false
positive there would permanently hide the Dashboard tab of a slow-booting
agent, with no retry able to recover it.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = str(Path(__file__).resolve().parents[2] / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import httpx  # noqa: E402

from services.agent_service import dashboard as dash  # noqa: E402


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _install(monkeypatch, *, agent_reply=None, raises=None, cached=None,
             container_status="running"):
    """Wire the module's four collaborators; return the logic call."""
    monkeypatch.setattr(dash, "get_agent_container",
                        lambda name: SimpleNamespace(status=container_status))

    async def _reload(_container):
        return None
    monkeypatch.setattr(dash, "container_reload", _reload)

    class _Client:
        async def get(self, _url):
            if raises is not None:
                raise raises
            return agent_reply

    @asynccontextmanager
    async def _factory(_name, timeout=10.0):
        yield _Client()
    monkeypatch.setattr(dash, "agent_httpx_client", _factory)

    monkeypatch.setattr(dash.db, "can_user_access_agent", lambda u, a: True)
    monkeypatch.setattr(dash.db, "get_cached_dashboard", lambda a: cached)
    monkeypatch.setattr(dash.db, "cache_valid_dashboard",
                        lambda *a, **k: None)
    monkeypatch.setattr(dash.db, "get_last_captured_mtime", lambda a: None)
    monkeypatch.setattr(dash.db, "capture_dashboard_snapshot",
                        lambda *a, **k: None)


_USER = SimpleNamespace(username="admin")


async def _call():
    return await dash.get_agent_dashboard_logic(
        "acme-sage", _USER,
        include_history=False, include_platform_metrics=False,
    )


@pytest.mark.asyncio
async def test_agent_says_no_dashboard_is_settled(monkeypatch):
    """The exact live shape: 200 + has_dashboard false + an error string.

    Captured from the running instance, agent `acme-sage`. This is a FINAL
    answer and must stop the retry ladder.
    """
    _install(monkeypatch, agent_reply=_Resp(200, {
        "has_dashboard": False,
        "config": None,
        "last_modified": None,
        "error": "No dashboard.yaml found at /home/developer/dashboard.yaml",
    }))
    out = await _call()
    assert out["has_dashboard"] is False
    assert out["settled"] is True, "an agent that answered must not be re-probed"


@pytest.mark.asyncio
async def test_valid_dashboard_is_settled(monkeypatch):
    _install(monkeypatch, agent_reply=_Resp(200, {
        "has_dashboard": True,
        "config": {"title": "T", "sections": [{"widgets": []}]},
        "last_modified": "2026-08-15T00:00:00Z",
    }))
    out = await _call()
    assert out["has_dashboard"] is True
    assert out["settled"] is True


@pytest.mark.asyncio
async def test_timeout_is_not_settled(monkeypatch):
    """A booting agent MUST keep its retries — this is the fail-safe direction."""
    _install(monkeypatch, raises=httpx.TimeoutException("boot"))
    out = await _call()
    assert out["has_dashboard"] is False
    assert "settled" not in out


@pytest.mark.asyncio
async def test_connection_error_is_not_settled(monkeypatch):
    _install(monkeypatch, raises=httpx.ConnectError("refused"))
    out = await _call()
    assert "settled" not in out


@pytest.mark.asyncio
async def test_non_200_is_not_settled(monkeypatch):
    """A 502 from the agent is not evidence about dashboard.yaml."""
    _install(monkeypatch, agent_reply=_Resp(502))
    out = await _call()
    assert "settled" not in out


@pytest.mark.asyncio
async def test_stopped_agent_is_not_settled(monkeypatch):
    """A stopped agent's `has_dashboard: false` says nothing about the file."""
    _install(monkeypatch, container_status="exited")
    out = await _call()
    assert out["status"] == "stopped"
    assert "settled" not in out


@pytest.mark.asyncio
async def test_stale_cache_served_on_failure_still_shows_the_tab(monkeypatch):
    """Unchanged behaviour guard: a cached config still wins over a blip.

    `stale` already stops the ladder on the frontend, so this path does not
    need `settled` — but it must keep working, or a transient error would hide
    a real dashboard.
    """
    _install(monkeypatch, raises=httpx.TimeoutException("blip"),
             cached={"has_dashboard": True, "config": {"title": "T"},
                     "last_modified": None, "error": None})
    out = await _call()
    assert out["stale"] is True
    assert out["has_dashboard"] is True
