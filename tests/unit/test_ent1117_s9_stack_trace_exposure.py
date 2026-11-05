"""py/stack-trace-exposure + abilityai/trinity#4284 on the agent-proxy, Telegram,
subscription and credential-settings routes (abilityai/trinity-enterprise#1117).

Each test drives the REAL handler with a planted sentinel in the exception
message and asserts three things:

(a) the sentinel is absent from the response (body, SSE frame, or
    ``HTTPException.detail``) — the caller never sees the exception text;
(b) the exception CLASS NAME is present — a response that dropped the error
    entirely would pass (a) while making the failure undiagnosable. The one
    deliberate exception is the unauthenticated public-link stream, which
    answers its existing fixed visitor sentence and names nothing;
(c) the backend log record carries the sentinel — the detail moved to the log,
    it was not lost. Where the handler holds a credential (a Telegram bot
    token travels in the request URL; an API key or PAT under test; a Slack
    secret), the exception also carries that credential and the log must NOT:
    moving the text from the response into the Vector-captured log must not
    move the secret with it.

The #1917 file (`test_1917_stack_trace_exposure.py`) covers the ops /
system-agent routes; this one covers a separate set of files on purpose.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

# A string no platform-authored message would ever contain.
SENTINEL = "sekrit-internal-host-10.0.0.7:5432"
# Credential-shaped values the handler holds; each must be absent from the log.
BOT_TOKEN = "123456789:AAH-s9-fake-bot-token-zzzzzzzzzzzzzzz"
ANTHROPIC_KEY = "sk-ant-api03-s9fakekeyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
GITHUB_PAT = "ghp_s9fakepatAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
SUB_TOKEN = "sk-ant-oat01-s9faketokenAAAAAAAAAAAAAAAAAAAAAAAAAAA"
SLACK_SECRET = "s9-fake-slack-client-secret-value-0001"
SLACK_SIGNING = "s9-fake-slack-signing-secret-value-0002"
SLACK_APP_TOKEN = "xapp-1-s9-fake-app-token-value-0003"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _blob(obj) -> str:
    return json.dumps(obj, default=str)


def _log_text(caplog) -> str:
    """Every captured record, formatted the way a handler would emit it —
    traceback included, so `exc_info=True` records count."""
    fmt = logging.Formatter("%(name)s %(levelname)s %(message)s")
    return "\n".join(fmt.format(r) for r in caplog.records)


def _live(name: str):
    """The module object the handler actually runs in (sys.modules), never a
    package attribute that can drift from it under the unit conftest."""
    __import__(name)
    return sys.modules[name]


def _admin():
    u = MagicMock()
    u.username = "admin"
    u.role = "admin"
    u.connector_agent = None
    u.agent_name = None
    u.mcp_scope = None
    return u


class _RaisingHttpx:
    """Stands in for `httpx.AsyncClient(...)`: every request raises `exc`."""

    def __init__(self, exc):
        self.exc = exc

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        raise self.exc

    async def post(self, *a, **k):
        raise self.exc

    def stream(self, *a, **k):
        raise self.exc


def _sse_frames(chunks) -> list[dict]:
    out = []
    for c in chunks:
        for line in c.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: "):]))
    return out


# ---------------------------------------------------------------------------
# #43 routers/public.py — the unauthenticated public-link stream
# ---------------------------------------------------------------------------

def test_public_stream_error_frame_is_the_fixed_visitor_sentence(monkeypatch, caplog):
    public = _live("routers.public")
    monkeypatch.setattr(public, "_get_client_ip", lambda r: "1.2.3.4")
    monkeypatch.setattr(public, "check_public_link_rate_limit", lambda ip: None)
    monkeypatch.setattr(public, "_validate_public_link", lambda t: {"agent_name": "a1"})
    monkeypatch.setattr(public, "get_agent_container", lambda n: SimpleNamespace(status="running"))
    monkeypatch.setattr(public, "db", MagicMock(get_execution=lambda e: SimpleNamespace(agent_name="a1")))
    monkeypatch.setattr(public, "pull_queue_allowance", lambda n: 0)
    monkeypatch.setattr(public, "agent_httpx_client", _RaisingHttpx(httpx.RemoteProtocolError(SENTINEL)))
    caplog.set_level(logging.ERROR)

    async def _go():
        resp = await public.public_stream_execution(token="t", execution_id="e1", request=MagicMock())
        return [c async for c in resp.body_iterator]

    chunks = _run(_go())
    frames = _sse_frames(chunks)

    assert SENTINEL not in "".join(chunks), chunks
    assert frames[0] == {"type": "error", "message": public._PUBLIC_FAILURE_MESSAGE}
    assert frames[-1] == {"type": "stream_end"}
    # An anonymous visitor is told nothing about the platform, not even a class.
    assert "RemoteProtocolError" not in "".join(chunks)
    logs = _log_text(caplog)
    assert SENTINEL in logs and "RemoteProtocolError" in logs


# ---------------------------------------------------------------------------
# #29 routers/chat.py — the authenticated execution stream
# ---------------------------------------------------------------------------

def test_chat_stream_error_frame_carries_no_raw_message(monkeypatch, caplog):
    chat = _live("routers.chat")
    monkeypatch.setattr(chat, "viewer_row_scope", lambda u, a: None)
    monkeypatch.setattr(chat, "get_agent_container", lambda n: SimpleNamespace(status="running"))
    monkeypatch.setattr(chat, "pull_queue_allowance", lambda n: 0)
    monkeypatch.setattr(chat, "agent_httpx_client", _RaisingHttpx(httpx.RemoteProtocolError(SENTINEL)))
    caplog.set_level(logging.ERROR)

    async def _go():
        resp = await chat.stream_execution_log(
            execution_id="e1", name="a1", current_user=SimpleNamespace(id=1))
        return [c async for c in resp.body_iterator]

    chunks = _run(_go())
    frames = _sse_frames(chunks)

    assert SENTINEL not in "".join(chunks), chunks
    assert frames[0]["type"] == "error"
    assert "RemoteProtocolError" in frames[0]["message"]
    # ExecutionDetail.vue keys on `retryable` being falsy to show the message.
    assert not frames[0].get("retryable")
    assert frames[-1] == {"type": "stream_end"}
    assert SENTINEL in _log_text(caplog)


# ---------------------------------------------------------------------------
# #25 + #4284 routers/agent_files.py
# ---------------------------------------------------------------------------

def test_agent_info_message_carries_no_raw_message(monkeypatch, caplog):
    af = _live("routers.agent_files")
    container = SimpleNamespace(status="running", labels={"trinity.template": "t"})
    monkeypatch.setattr(af, "get_agent_container", lambda n: container)
    monkeypatch.setattr(af, "container_reload", AsyncMock())
    monkeypatch.setattr(af, "agent_httpx_client", _RaisingHttpx(RuntimeError(SENTINEL)))
    caplog.set_level(logging.WARNING)

    out = _run(af.get_agent_info_endpoint(agent_name="a1", request=MagicMock()))

    assert SENTINEL not in _blob(out)
    assert out["message"].startswith("Could not fetch template info")
    assert "RuntimeError" in out["message"]
    assert out["status"] == "running"
    assert SENTINEL in _log_text(caplog)


def test_agent_playbooks_500_detail_carries_no_raw_message(monkeypatch, caplog):
    af = _live("routers.agent_files")

    async def _boom(name, last_known=False):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(af.agent_skills_listing, "list_skills", _boom)
    caplog.set_level(logging.WARNING)

    with pytest.raises(HTTPException) as ei:
        _run(af.get_agent_playbooks_endpoint(agent_name="a1", request=MagicMock(), last_known=False))

    assert ei.value.status_code == 500
    assert SENTINEL not in str(ei.value.detail)
    assert ei.value.detail.startswith("Failed to fetch playbooks")
    assert "RuntimeError" in ei.value.detail
    assert SENTINEL in _log_text(caplog)


def test_agent_playbooks_named_refusal_is_untouched(monkeypatch):
    """The deliberate domain answer (status + detail from the listing) passes
    through as before — only the catch-all changed."""
    af = _live("routers.agent_files")

    async def _refuse(name, last_known=False):
        raise af.agent_skills_listing.SkillsListUnavailable("not_running", 503, "Agent is not running")

    monkeypatch.setattr(af.agent_skills_listing, "list_skills", _refuse)
    with pytest.raises(HTTPException) as ei:
        _run(af.get_agent_playbooks_endpoint(agent_name="a1", request=MagicMock(), last_known=False))
    assert (ei.value.status_code, ei.value.detail) == (503, "Agent is not running")


# ---------------------------------------------------------------------------
# #52 #53 (+ the 502 sibling) routers/telegram.py — the token is in the URL
# ---------------------------------------------------------------------------

def _url_echoing_error(path: str) -> httpx.ConnectError:
    # The shape a URL-echoing failure takes (httpx's HTTPStatusError puts the
    # request URL in its message, and the bot token is a path segment).
    return httpx.ConnectError(
        f"{SENTINEL} for url 'https://api.telegram.org/bot{BOT_TOKEN}/{path}'"
    )


@pytest.mark.parametrize("chat_id,path", [(None, "getMe"), ("42", "sendMessage")])
def test_telegram_test_bot_message_carries_no_raw_message_or_token(monkeypatch, caplog, chat_id, path):
    tg = _live("routers.telegram")
    models = _live("models")
    monkeypatch.setattr(tg, "db", SimpleNamespace(get_telegram_bot_token=lambda n: BOT_TOKEN))
    monkeypatch.setattr(tg.httpx, "AsyncClient", _RaisingHttpx(_url_echoing_error(path)))
    caplog.set_level(logging.WARNING)

    out = _run(tg.test_telegram_bot(
        agent_name="a1", test=models.TelegramTestRequest(chat_id=chat_id)))

    body = _blob(out)
    assert out["ok"] is False
    assert SENTINEL not in body and BOT_TOKEN not in body, body
    assert "ConnectError" in out["message"]
    logs = _log_text(caplog)
    assert SENTINEL in logs, "the detail must reach the backend log"
    assert BOT_TOKEN not in logs, "the bot token must be masked in the log"
    assert BOT_TOKEN.split(":", 1)[1] not in logs


def test_telegram_configure_502_detail_carries_no_raw_message_or_token(monkeypatch, caplog):
    tg = _live("routers.telegram")
    models = _live("models")
    monkeypatch.setattr(tg.httpx, "AsyncClient", _RaisingHttpx(_url_echoing_error("getMe")))
    caplog.set_level(logging.WARNING)

    with pytest.raises(HTTPException) as ei:
        _run(tg.configure_telegram_bot(
            agent_name="a1", config=models.TelegramConfigureRequest(bot_token=f"  {BOT_TOKEN} ")))

    assert ei.value.status_code == 502
    detail = str(ei.value.detail)
    assert SENTINEL not in detail and BOT_TOKEN not in detail
    assert detail.startswith("Could not reach Telegram API")
    assert "ConnectError" in detail
    logs = _log_text(caplog)
    assert SENTINEL in logs and BOT_TOKEN not in logs
