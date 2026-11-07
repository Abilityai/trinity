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
import re
import socket
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
# Held values pasted with a line break. `sanitize_text` recognises at most the
# head of each (pinned by the first test below), so only the held-value mask
# keeps the tail out of the log, whatever form the exception renders it in.
KEY_TAIL = "T" * 24 + "keytailZ"
SPLIT_KEY = "sk-ant-api03-" + "K" * 24 + "\n" + KEY_TAIL
PAT_TAIL = "Q" * 24 + "pattailZ"
SPLIT_PAT = "ghp_" + "P" * 36 + "\n" + PAT_TAIL
TOKEN_TAIL = "O" * 24 + "subtailZ"
SPLIT_SUB_TOKEN = "sk-ant-oat01-" + "S" * 24 + "\n" + TOKEN_TAIL
_REAL_ASYNC_CLIENT = httpx.AsyncClient


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


def _pieces(held: str) -> list[str]:
    """The held value plus every run of 8+ characters that no escaping
    rewrites: none of them may appear where the value must not."""
    return [held, *re.findall(r"[A-Za-z0-9_.~-]{8,}", held)]


def _assert_traceback_logged(caplog, prefix: str) -> None:
    """The record this site writes carries the traceback, not only `str(e)`
    (an `exc_info=True` record, or a masked traceback in the message)."""
    fmt = logging.Formatter("%(message)s")
    hits = [fmt.format(r) for r in caplog.records if r.getMessage().startswith(prefix)]
    assert hits, f"no log record starts with {prefix!r}"
    assert any("Traceback (most recent call last)" in h for h in hits), hits


def test_the_split_values_are_not_recognised_by_sanitize_text():
    from utils.credential_sanitizer import sanitize_text

    for tail in (KEY_TAIL, PAT_TAIL, TOKEN_TAIL, SLACK_SECRET, SLACK_SIGNING, SLACK_APP_TOKEN):
        assert tail in sanitize_text(f"x {tail} y")


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
    _assert_traceback_logged(caplog, "[PublicStream] Error streaming from agent")


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
    _assert_traceback_logged(caplog, "[Stream] Error streaming from agent")


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
    _assert_traceback_logged(caplog, "Could not fetch template info for agent")


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
    _assert_traceback_logged(caplog, "Failed to fetch playbooks for agent")


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
    _assert_traceback_logged(caplog, "Could not ")


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


# ---------------------------------------------------------------------------
# #50 #51 (+ #4284) routers/subscriptions.py, services/subscription_auto_switch.py
# ---------------------------------------------------------------------------

@pytest.fixture
def sub_env(monkeypatch):
    """The subscription router with its db, audit and container seams stubbed.
    `start_agent_internal` raises the sentinel, so every restart path fails."""
    rs = _live("routers.subscriptions")
    fake_db = MagicMock()
    fake_db.can_user_share_agent.return_value = True
    sub = MagicMock()
    sub.id = "sub-b"
    sub.name = "sub-B"
    fake_db.get_subscription_by_name.return_value = sub
    fake_db.get_agent_subscription.return_value = sub
    monkeypatch.setattr(rs, "db", fake_db)
    monkeypatch.setitem(rs.assert_agent_owner.__globals__, "db", fake_db)
    monkeypatch.setattr(rs, "platform_audit_service", MagicMock(log=AsyncMock()))

    docker_service = types.ModuleType("services.docker_service")
    docker_service.get_agent_container = lambda name: object()
    docker_service.get_agent_status_from_container = lambda c: SimpleNamespace(status="running")
    monkeypatch.setitem(sys.modules, "services.docker_service", docker_service)
    docker_utils = types.ModuleType("services.docker_utils")
    docker_utils.container_stop = AsyncMock()
    monkeypatch.setitem(sys.modules, "services.docker_utils", docker_utils)
    agent_service = types.ModuleType("services.agent_service")

    async def _start(name):
        raise RuntimeError(SENTINEL)

    agent_service.start_agent_internal = _start
    monkeypatch.setitem(sys.modules, "services.agent_service", agent_service)

    auto_switch = _live("services.subscription_auto_switch")

    async def _lock(name):
        return asyncio.Lock()

    monkeypatch.setattr(auto_switch, "agent_switch_lock", _lock)
    return SimpleNamespace(rs=rs, db=fake_db, auto_switch=auto_switch)


def _owner():
    u = _admin()
    u.username = "owner"
    u.role = "user"
    return u


def _assert_failed_restart(value: str):
    # `failed:` is a contract: test_subscription_auto_switch_pingpong and the
    # SUB-003 walk read the prefix; the class name keeps it diagnosable.
    assert value.startswith("failed:"), value
    assert SENTINEL not in value
    assert "RuntimeError" in value


def test_restart_agent_result_carries_no_raw_message(sub_env, caplog):
    caplog.set_level(logging.ERROR)
    out = _run(sub_env.auto_switch._restart_agent("a1"))
    _assert_failed_restart(out)
    assert SENTINEL in _log_text(caplog)
    _assert_traceback_logged(caplog, "[SUB-003] Failed to restart agent")


def test_assign_mode_change_restart_failure_carries_no_raw_message(sub_env, caplog):
    sub_env.db.get_agent_subscription_id.return_value = None  # api-key → subscription
    caplog.set_level(logging.ERROR)

    out = _run(sub_env.rs.assign_subscription_to_agent(
        agent_name="a1", subscription_name="sub-B", current_user=_owner()))

    assert SENTINEL not in _blob(out), out
    _assert_failed_restart(out["restart_result"])
    assert out["injection_result"]["status"] == "failed"
    assert "RuntimeError" in out["injection_result"]["error"]
    assert SENTINEL in _log_text(caplog)
    _assert_traceback_logged(caplog, "Failed to restart agent 'a1' for subscription")


def test_assign_hot_reload_fallback_restart_failure_carries_no_raw_message(sub_env, monkeypatch, caplog):
    """The second SARIF source of #50: sub→sub hot-reload falls back to
    `_restart_agent`, whose result becomes both `restart_result` and
    `injection_result.status`."""
    sub_env.db.get_agent_subscription_id.return_value = "sub-a"  # sub → sub
    real_restart = sub_env.auto_switch._restart_agent

    async def _hot(name):  # the no-token / 404 / transport branches all do this
        return await real_restart(name)

    monkeypatch.setattr(sub_env.auto_switch, "_hot_reload_subscription_token", _hot)
    caplog.set_level(logging.ERROR)

    out = _run(sub_env.rs.assign_subscription_to_agent(
        agent_name="a1", subscription_name="sub-B", current_user=_owner()))

    assert SENTINEL not in _blob(out), out
    _assert_failed_restart(out["restart_result"])
    _assert_failed_restart(out["injection_result"]["status"])
    assert SENTINEL in _log_text(caplog)


def test_clear_restart_failure_carries_no_raw_message(sub_env, caplog):
    caplog.set_level(logging.ERROR)

    out = _run(sub_env.rs.clear_agent_subscription(agent_name="a1", current_user=_owner()))

    assert SENTINEL not in _blob(out), out
    _assert_failed_restart(out["restart_result"])
    assert SENTINEL in _log_text(caplog)
    _assert_traceback_logged(caplog, "Failed to restart agent 'a1' after subscription removal")


@pytest.mark.parametrize("token", [SUB_TOKEN, SPLIT_SUB_TOKEN], ids=["token", "token-with-line-break"])
def test_register_500_detail_carries_no_raw_message_or_token(sub_env, monkeypatch, caplog, token):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "0" * 64)
    sub_env.db.get_user_by_username.return_value = {"id": 1}
    # A DB error renders its bound parameters as a repr (the SQLAlchemy shape).
    sub_env.db.create_subscription.side_effect = RuntimeError(
        f"{SENTINEL} [parameters: {('s', token)!r}]")
    ss = _live("services.subscription_service")
    monkeypatch.setattr(ss, "is_claude_auth_configured", lambda: True)
    db_models = _live("db_models")
    payload = db_models.SubscriptionCredentialCreate(name="s", token=token)
    caplog.set_level(logging.ERROR)

    with pytest.raises(HTTPException) as ei:
        _run(sub_env.rs.register_subscription(
            payload=payload, http_request=MagicMock(), current_user=_admin()))

    assert ei.value.status_code == 500
    assert SENTINEL not in ei.value.detail
    assert not any(p in ei.value.detail for p in _pieces(token))
    assert ei.value.detail.startswith("Failed to register subscription")
    assert "RuntimeError" in ei.value.detail
    logs = _log_text(caplog)
    assert SENTINEL in logs
    leaked = [p for p in _pieces(token) if p in logs]
    assert not leaked, f"the token must be masked in the log: {leaked}"
    _assert_traceback_logged(caplog, "Failed to register subscription")


def test_register_value_error_stays_a_400_with_its_message(sub_env, monkeypatch):
    """A deliberate domain message (ValueError → 400) is unchanged."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "0" * 64)
    sub_env.db.get_user_by_username.return_value = {"id": 1}
    sub_env.db.create_subscription.side_effect = ValueError("name already taken")
    ss = _live("services.subscription_service")
    monkeypatch.setattr(ss, "is_claude_auth_configured", lambda: True)
    db_models = _live("db_models")
    payload = db_models.SubscriptionCredentialCreate(name="s", token=SUB_TOKEN)

    with pytest.raises(HTTPException) as ei:
        _run(sub_env.rs.register_subscription(
            payload=payload, http_request=MagicMock(), current_user=_admin()))
    assert (ei.value.status_code, ei.value.detail) == (400, "name already taken")


def test_agent_auth_status_500_detail_carries_no_raw_message(sub_env, monkeypatch, caplog):
    monkeypatch.setattr(sub_env.rs, "assert_agent_access", lambda *a, **k: None)
    ss = _live("services.subscription_service")

    async def _boom(name):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(ss, "get_agent_auth_mode", _boom)
    caplog.set_level(logging.ERROR)

    with pytest.raises(HTTPException) as ei:
        _run(sub_env.rs.get_agent_auth_status(agent_name="a1", current_user=_owner()))

    assert ei.value.status_code == 500
    assert SENTINEL not in ei.value.detail
    assert "RuntimeError" in ei.value.detail
    assert SENTINEL in _log_text(caplog)
    _assert_traceback_logged(caplog, "Failed to get auth status for agent")


# ---------------------------------------------------------------------------
# #347 #348 #390 (+ #4284) routers/settings/credentials.py
# ---------------------------------------------------------------------------

@pytest.fixture
def cr(monkeypatch):
    mod = _live("routers.settings.credentials")
    monkeypatch.setattr(mod, "assert_admin", lambda user, **kw: None)
    monkeypatch.setattr(mod, "platform_audit_service", MagicMock(log=AsyncMock()))
    return mod


def _request():
    r = MagicMock()
    r.client.host = "127.0.0.1"
    r.scope = {"path": "/api/settings/x"}
    return r


def _assert_clean(text: str, held: str | None = None, cls: str = "RuntimeError"):
    assert SENTINEL not in text, text
    if held:
        assert not [p for p in _pieces(held) if p in text], text
    assert cls in text, text


def _assert_logged(caplog, held: str | None = None, detail: str = SENTINEL):
    logs = _log_text(caplog)
    assert detail in logs, "the detail must reach the backend log"
    if held:
        leaked = [p for p in _pieces(held) if p in logs]
        assert not leaked, f"the credential must be masked in the log: {leaked}"


@pytest.fixture
def local_httpx(monkeypatch):
    """Every `httpx.AsyncClient` the handler opens is a REAL client whose
    requests go to a local listening socket. httpx and h11 then validate the
    handler's own headers, so an illegal header value raises the real error
    (`Illegal header value b'...'`) through the real `except` branch. The
    socket's backlog completes the handshake; nothing is ever answered."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    class _ToLocal(httpx.AsyncBaseTransport):
        def __init__(self):
            self._inner = httpx.AsyncHTTPTransport()

        async def handle_async_request(self, request):
            request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=port)
            return await self._inner.handle_async_request(request)

        async def aclose(self):
            await self._inner.aclose()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _REAL_ASYNC_CLIENT(*a, transport=_ToLocal(), **k))
    yield
    srv.close()


def _real_header_error(name: str, value: str) -> Exception:
    """The exception a real httpx client raises for this header value."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    async def go():
        async with _REAL_ASYNC_CLIENT() as client:
            await client.get(f"http://127.0.0.1:{port}/", headers={name: value}, timeout=2)

    try:
        _run(go())
    except Exception as e:  # noqa: BLE001 — the error under test
        return e
    finally:
        srv.close()
    raise AssertionError("httpx accepted the header")


def test_anthropic_key_with_a_line_break_logs_no_part_of_the_key(cr, local_httpx, caplog):
    """A key pasted with a line break passes the prefix-only format check and
    reaches the request header; httpx refuses it and echoes it as a bytes repr."""
    caplog.set_level(logging.WARNING)
    out = _run(cr.test_anthropic_key(
        body=cr.ApiKeyTest(api_key=SPLIT_KEY), request=_request(), current_user=_admin()))
    assert out["valid"] is False
    _assert_clean(_blob(out), SPLIT_KEY, cls="LocalProtocolError")
    _assert_logged(caplog, SPLIT_KEY, detail="Illegal header value")
    _assert_traceback_logged(caplog, "Error testing key")


def test_github_pat_with_a_line_break_logs_no_part_of_the_pat(cr, local_httpx, caplog):
    caplog.set_level(logging.WARNING)
    out = _run(cr.test_github_pat(
        body=cr.ApiKeyTest(api_key=SPLIT_PAT), request=_request(), current_user=_admin()))
    assert out["valid"] is False
    _assert_clean(_blob(out), SPLIT_PAT, cls="LocalProtocolError")
    _assert_logged(caplog, SPLIT_PAT, detail="Illegal header value")
    _assert_traceback_logged(caplog, "Error testing token")


def test_github_pat_propagation_header_error_logs_no_part_of_the_pat(cr, monkeypatch, caplog):
    monkeypatch.setattr(cr, "set_secret_setting", lambda k, v: None)
    prop = _live("services.github_pat_propagation_service")
    exc = _real_header_error("Authorization", f"Bearer {SPLIT_PAT.strip()}")

    async def _boom(key):
        raise exc

    monkeypatch.setattr(prop, "propagate_github_pat", _boom)
    caplog.set_level(logging.WARNING)

    out = _run(cr.update_github_pat(
        body=cr.ApiKeyUpdate(api_key=SPLIT_PAT), request=_request(), current_user=_admin()))

    assert out["success"] is True and out["propagation"]["error"]
    _assert_clean(_blob(out), SPLIT_PAT, cls="LocalProtocolError")
    _assert_logged(caplog, SPLIT_PAT, detail="Illegal header value")


def test_a_short_held_value_does_not_garble_the_log(cr, monkeypatch, caplog):
    def _boom(*a, **k):
        raise RuntimeError("database is locked at host db-primary")

    monkeypatch.setattr(cr, "db", MagicMock())
    monkeypatch.setattr(cr, "set_secret_setting", _boom)
    caplog.set_level(logging.WARNING)
    with pytest.raises(HTTPException):
        _run(cr.update_slack_settings(
            body=cr.SlackSettingsUpdate(client_secret="a"), request=_request(), current_user=_admin()))
    assert "database is locked at host db-primary" in _log_text(caplog)


def test_anthropic_key_test_error_carries_no_raw_message_or_key(cr, monkeypatch, caplog):
    monkeypatch.setattr(cr.httpx, "AsyncClient", _RaisingHttpx(RuntimeError(f"{SENTINEL} {ANTHROPIC_KEY}")))
    caplog.set_level(logging.WARNING)
    out = _run(cr.test_anthropic_key(
        body=cr.ApiKeyTest(api_key=f" {ANTHROPIC_KEY} "), request=_request(), current_user=_admin()))
    assert out["valid"] is False
    assert out["error"].startswith("Error testing key")
    _assert_clean(_blob(out), ANTHROPIC_KEY)
    _assert_logged(caplog, ANTHROPIC_KEY)


def test_github_pat_test_error_carries_no_raw_message_or_pat(cr, monkeypatch, caplog):
    monkeypatch.setattr(cr.httpx, "AsyncClient", _RaisingHttpx(RuntimeError(f"{SENTINEL} Bearer {GITHUB_PAT}")))
    caplog.set_level(logging.WARNING)
    out = _run(cr.test_github_pat(
        body=cr.ApiKeyTest(api_key=GITHUB_PAT), request=_request(), current_user=_admin()))
    assert out["valid"] is False
    assert out["error"].startswith("Error testing token")
    _assert_clean(_blob(out), GITHUB_PAT)
    _assert_logged(caplog, GITHUB_PAT)


def test_github_pat_propagation_error_keeps_the_key_and_drops_the_text(cr, monkeypatch, caplog):
    monkeypatch.setattr(cr, "set_secret_setting", lambda k, v: None)
    prop = _live("services.github_pat_propagation_service")

    async def _boom(key):
        raise RuntimeError(f"{SENTINEL} https://x-access-token:{GITHUB_PAT}@github.com/o/r")

    monkeypatch.setattr(prop, "propagate_github_pat", _boom)
    caplog.set_level(logging.WARNING)

    out = _run(cr.update_github_pat(
        body=cr.ApiKeyUpdate(api_key=GITHUB_PAT), request=_request(), current_user=_admin()))

    assert out["success"] is True
    # Settings.vue renders "PAT saved, but propagation failed: {{ error }}" when
    # `propagation.error` is truthy — the key must stay.
    assert out["propagation"]["error"]
    _assert_clean(_blob(out), GITHUB_PAT)
    _assert_logged(caplog, GITHUB_PAT)


def _raise(*a, **k):
    raise RuntimeError(SENTINEL)


def _raise_with(held):
    # Raw, and as a repr: a DB or client error renders its arguments either way.
    def _f(*a, **k):
        raise RuntimeError(f"{SENTINEL} {held} args=({held!r},)")
    return _f


def _case_api_keys_status(cr, monkeypatch):
    monkeypatch.setattr(cr, "get_anthropic_api_key", _raise)
    return cr.get_api_keys_status(request=_request(), current_user=_admin()), None, "Failed to get API keys status"


def _case_update_anthropic(cr, monkeypatch):
    monkeypatch.setattr(cr, "is_claude_auth_configured", lambda: True)
    monkeypatch.setattr(cr, "set_secret_setting", _raise_with(SPLIT_KEY))
    body = cr.ApiKeyUpdate(api_key=SPLIT_KEY)
    return cr.update_anthropic_key(body=body, request=_request(), current_user=_admin()), SPLIT_KEY, "Failed to update API key"


def _case_delete_anthropic(cr, monkeypatch):
    monkeypatch.setattr(cr, "clear_secret_setting", _raise)
    return cr.delete_anthropic_key(request=_request(), current_user=_admin()), None, "Failed to delete API key"


def _case_update_github(cr, monkeypatch):
    monkeypatch.setattr(cr, "set_secret_setting", _raise_with(SPLIT_PAT))
    body = cr.ApiKeyUpdate(api_key=SPLIT_PAT)
    return cr.update_github_pat(body=body, request=_request(), current_user=_admin()), SPLIT_PAT, "Failed to update GitHub PAT"


def _case_delete_github(cr, monkeypatch):
    monkeypatch.setattr(cr, "clear_secret_setting", _raise)
    return cr.delete_github_pat(request=_request(), current_user=_admin()), None, "Failed to delete GitHub PAT"


def _case_get_slack(cr, monkeypatch):
    ss = _live("services.settings_service")
    monkeypatch.setattr(ss, "get_slack_client_id", _raise)
    return cr.get_slack_settings_status(request=_request(), current_user=_admin()), None, "Failed to get Slack settings"


def _case_update_slack(cr, monkeypatch):
    monkeypatch.setattr(cr, "db", MagicMock())
    monkeypatch.setattr(cr, "set_secret_setting", _raise_with(f"{SLACK_SECRET} {SLACK_SIGNING}"))
    body = cr.SlackSettingsUpdate(client_secret=SLACK_SECRET, signing_secret=SLACK_SIGNING)
    return cr.update_slack_settings(body=body, request=_request(), current_user=_admin()), SLACK_SECRET, "Failed to update Slack settings"


def _case_delete_slack(cr, monkeypatch):
    monkeypatch.setattr(cr, "db", MagicMock(delete_setting=_raise))
    return cr.delete_slack_settings(request=_request(), current_user=_admin()), None, "Failed to delete Slack settings"


def _case_connect_slack(cr, monkeypatch):
    monkeypatch.setattr(cr, "set_secret_setting", lambda k, v: None)
    ss = _live("services.settings_service")
    monkeypatch.setattr(ss, "get_slack_transport_mode", lambda: "socket")
    monkeypatch.setattr(ss, "get_slack_app_token", lambda: SLACK_APP_TOKEN)
    for name in ("adapters.slack_adapter", "adapters.message_router", "adapters.transports.slack_socket"):
        fake = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, fake)
    sys.modules["adapters.slack_adapter"].SlackAdapter = MagicMock
    sys.modules["adapters.message_router"].message_router = MagicMock()

    class _Transport:
        def __init__(self, *a):
            pass

        async def start(self):
            raise RuntimeError(f"{SENTINEL} {SLACK_APP_TOKEN}")

    sys.modules["adapters.transports.slack_socket"].SlackSocketTransport = _Transport
    request = _request()
    request.app.state = SimpleNamespace(slack_transport=None)
    body = cr.SlackConnectRequest(app_token=SLACK_APP_TOKEN)
    return cr.connect_slack_transport(request=request, body=body, current_user=_admin()), SLACK_APP_TOKEN, "Failed to connect"


def _slack_connect_with(cr, monkeypatch, *, mode, held, body):
    """connect_slack_transport whose transport start echoes ``held``: the
    value the handler read from settings, not one the request carried."""
    monkeypatch.setattr(cr, "set_secret_setting", lambda k, v: None)
    ss = _live("services.settings_service")
    monkeypatch.setattr(ss, "get_slack_transport_mode", lambda: mode)
    monkeypatch.setattr(ss, "get_slack_app_token", lambda: held)
    monkeypatch.setattr(ss, "get_slack_signing_secret", lambda: held)
    kind = "socket" if mode == "socket" else "webhook"
    for name in ("adapters.slack_adapter", "adapters.message_router", f"adapters.transports.slack_{kind}"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["adapters.slack_adapter"].SlackAdapter = MagicMock
    sys.modules["adapters.message_router"].message_router = MagicMock()

    class _Transport:
        def __init__(self, *a):
            pass

        async def start(self):
            raise RuntimeError(f"{SENTINEL} {held}")

    setattr(sys.modules[f"adapters.transports.slack_{kind}"],
            "SlackSocketTransport" if kind == "socket" else "SlackWebhookTransport", _Transport)
    request = _request()
    request.app.state = SimpleNamespace(slack_transport=None)
    return cr.connect_slack_transport(request=request, body=body, current_user=_admin()), held, "Failed to connect"


def _case_reconnect_slack_stored_app_token(cr, monkeypatch):
    # Reconnect: the body carries no token, so only the stored one is held.
    return _slack_connect_with(cr, monkeypatch, mode="socket", held=SLACK_APP_TOKEN,
                               body=cr.SlackConnectRequest())


def _case_connect_slack_webhook_signing_secret(cr, monkeypatch):
    return _slack_connect_with(cr, monkeypatch, mode="webhook", held=SLACK_SIGNING,
                               body=cr.SlackConnectRequest(transport_mode=None))


@pytest.mark.parametrize("case", [
    _case_api_keys_status, _case_update_anthropic, _case_delete_anthropic,
    _case_update_github, _case_delete_github, _case_get_slack,
    _case_update_slack, _case_delete_slack, _case_connect_slack,
    _case_reconnect_slack_stored_app_token, _case_connect_slack_webhook_signing_secret,
], ids=lambda f: f.__name__[len("_case_"):])
def test_settings_credentials_500_detail_carries_no_raw_message(cr, monkeypatch, caplog, case):
    coro, held, prefix = case(cr, monkeypatch)
    caplog.set_level(logging.WARNING)

    with pytest.raises(HTTPException) as ei:
        _run(coro)

    assert ei.value.status_code == 500
    detail = str(ei.value.detail)
    assert detail.startswith(prefix), detail
    _assert_clean(detail, held)
    _assert_logged(caplog, held)
    if held == SLACK_SECRET:
        assert SLACK_SIGNING not in _log_text(caplog)
    _assert_traceback_logged(caplog, prefix)


def test_update_anthropic_key_format_refusal_is_untouched(cr, monkeypatch):
    """A deliberate 400 domain message still passes through as written."""
    with pytest.raises(HTTPException) as ei:
        _run(cr.update_anthropic_key(
            body=cr.ApiKeyUpdate(api_key="not-a-key"), request=_request(), current_user=_admin()))
    assert ei.value.status_code == 400
