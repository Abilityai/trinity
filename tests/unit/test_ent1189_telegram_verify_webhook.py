"""Verify reports whether Telegram can deliver to this agent, and registers the
webhook when Telegram holds no webhook or one for another URL (ent#1189, B18).

"Operational" follows Telegram's own record (getWebhookInfo): its URL must be
this agent's webhook under the current, normalised Public URL, with no
delivery error outstanding. The stored `webhook_url` column is never cleared,
so it cannot answer that.
"""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import adapters.transports.telegram_webhook as tw
import routers.telegram as tg
from services import settings_service as ss

pytestmark = pytest.mark.unit

TOKEN = "123456:SECRET-bot-token-value"
SECRET = "s3cr3t"
BINDING = {"webhook_secret": SECRET, "webhook_url": "https://old.example.com/api/telegram/webhook/s3cr3t",
           "bot_username": "probe_bot", "bot_id": "1"}


def _hook(base):
    return f"{base}/api/telegram/webhook/{SECRET}"


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _Client:
    def __init__(self, body=None, exc=None):
        self.body, self.exc = body, exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def _call(self, *a, **kw):
        if self.exc:
            raise self.exc
        return _Resp(self.body)

    get = post = _call


def _settings(row):
    svc = ss.SettingsService()
    svc.get_setting = lambda key, default=None: row if key == "public_chat_url" else default
    return svc


def _verify(row, info, register_error=None, info_error=None):
    fake_db = MagicMock()
    fake_db.get_telegram_bot_token.return_value = TOKEN
    fake_db.get_telegram_binding.return_value = BINDING
    register = AsyncMock(return_value=register_error)
    get_info = AsyncMock(return_value=(info, info_error))
    with patch.object(tg, "db", fake_db), \
         patch.object(tg.httpx, "AsyncClient", lambda **kw: _Client({"ok": True, "result": {"username": "probe_bot"}})), \
         patch.object(tw, "set_webhook", register), \
         patch.object(tw, "get_webhook_info", get_info), \
         patch("services.settings_service.settings_service", _settings(row)):
        out = asyncio.run(tg.test_telegram_bot("probe", SimpleNamespace(chat_id=None, message="x")))
    return out, register


def test_telegram_holding_this_webhook_is_operational():
    out, register = _verify("https://x.example.com", {"url": _hook("https://x.example.com"), "pending_update_count": 0})
    assert out["ok"] is True and out["webhook_registered"] is True
    register.assert_not_awaited()


def test_no_public_url_is_not_operational_even_with_a_stored_webhook():
    out, register = _verify("", {"url": _hook("https://old.example.com")})
    assert out["ok"] is False and out["webhook_registered"] is False
    assert out["webhook_error"] == "no Public URL is set in Settings"
    assert "operational" not in out["message"]
    register.assert_not_awaited()


@pytest.mark.parametrize("telegram_url", ["", _hook("https://old.example.com")])
def test_missing_or_stale_webhook_is_reregistered(telegram_url):
    out, register = _verify("https://x.example.com", {"url": telegram_url})
    register.assert_awaited_once_with("probe", "https://x.example.com")
    assert out["ok"] is True and out["webhook_error"] is None


def test_registration_uses_the_normalised_public_url():
    """A row stored with a trailing host dot (#3881) is registered without it."""
    _, register = _verify("https://x.example.com.", {"url": ""})
    register.assert_awaited_once_with("probe", "https://x.example.com")


def test_rejected_registration_surfaces_telegram_reason():
    reason = "Telegram rejected https://x.example.com: Bad Request: bad webhook: IP address is reserved"
    out, _ = _verify("https://x.example.com", {"url": ""}, register_error=reason)
    assert out["ok"] is False
    assert out["webhook_error"] == reason
    assert reason in out["message"]


def test_outstanding_delivery_error_is_not_operational():
    info = {"url": _hook("https://x.example.com"), "pending_update_count": 3,
            "last_error_message": "SSL error {error:0A000086:SSL routines::certificate verify failed}"}
    out, register = _verify("https://x.example.com", info)
    assert out["ok"] is False and out["webhook_registered"] is False
    assert "certificate verify failed" in out["webhook_error"]
    register.assert_not_awaited()


def test_old_delivery_error_with_nothing_pending_is_operational():
    """Telegram keeps the last error after delivery recovers; with no update
    waiting, nothing is failing now."""
    info = {"url": _hook("https://x.example.com"), "pending_update_count": 0,
            "last_error_message": "Connection timed out"}
    out, _ = _verify("https://x.example.com", info)
    assert out["ok"] is True


def test_unreadable_webhook_info_is_not_operational():
    out, register = _verify("https://x.example.com", None, info_error="could not reach Telegram (ConnectError)")
    assert out["ok"] is False
    assert out["webhook_error"] == "could not reach Telegram (ConnectError)"
    register.assert_not_awaited()


# --- binding read: the stored URL counts only under the current Public URL ---

@pytest.mark.parametrize("row, shown", [
    ("https://old.example.com", BINDING["webhook_url"]),
    ("https://new.example.com", None),
    ("", None),
])
def test_binding_hides_a_webhook_stored_under_another_public_url(row, shown):
    fake_db = MagicMock()
    fake_db.get_telegram_binding.return_value = BINDING
    fake_db.get_telegram_groups_for_agent.return_value = []
    with patch.object(tg, "db", fake_db), \
         patch("services.settings_service.settings_service", _settings(row)):
        out = asyncio.run(tg.get_telegram_binding("probe"))
    assert out.webhook_url == shown
    assert out.public_url_set is bool(row)


# --- transport: reason strings and token hygiene ---

def _transport(monkeypatch, client):
    fake_db = MagicMock()
    fake_db.get_telegram_binding.return_value = {"webhook_secret": SECRET, "telegram_secret_token": "t"}
    fake_db.get_telegram_bot_token.return_value = TOKEN
    monkeypatch.setattr(tw, "db", fake_db)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    return fake_db


def test_set_webhook_returns_telegram_description(monkeypatch):
    _transport(monkeypatch, _Client({"ok": False, "description": "Bad Request: bad webhook: HTTPS url must be provided"}))
    reason = asyncio.run(tw.set_webhook("probe", "http://x.example.com"))
    assert reason == "Telegram rejected http://x.example.com: Bad Request: bad webhook: HTTPS url must be provided"


def test_set_webhook_success_stores_url(monkeypatch):
    fake_db = _transport(monkeypatch, _Client({"ok": True}))
    assert asyncio.run(tw.set_webhook("probe", "https://x.example.com")) is None
    fake_db.update_telegram_webhook_url.assert_called_once_with("probe", _hook("https://x.example.com"))


def _token_bearing_error():
    import httpx
    return httpx.ConnectError(f"failed to connect to https://api.telegram.org/bot{TOKEN}/setWebhook")


def test_set_webhook_network_error_keeps_token_out_of_reason_and_log(monkeypatch, caplog):
    _transport(monkeypatch, _Client(exc=_token_bearing_error()))
    with caplog.at_level(logging.DEBUG):
        reason = asyncio.run(tw.set_webhook("probe", "https://x.example.com"))
    assert reason == "could not reach Telegram (ConnectError)"
    assert "SECRET-bot-token-value" not in caplog.text
    assert all(not r.exc_info for r in caplog.records)


def test_get_webhook_info_returns_result(monkeypatch):
    _transport(monkeypatch, _Client({"ok": True, "result": {"url": "u"}}))
    assert asyncio.run(tw.get_webhook_info(TOKEN)) == ({"url": "u"}, None)


def test_get_webhook_info_network_error_keeps_token_out(monkeypatch, caplog):
    _transport(monkeypatch, _Client(exc=_token_bearing_error()))
    with caplog.at_level(logging.DEBUG):
        info, reason = asyncio.run(tw.get_webhook_info(TOKEN))
    assert info is None and reason == "could not reach Telegram (ConnectError)"
    assert "SECRET-bot-token-value" not in caplog.text
