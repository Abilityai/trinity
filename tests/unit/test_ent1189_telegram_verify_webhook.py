"""Verify reports the webhook, and registers it when it is missing.

Verify used to call only getMe, so it said "operational" while the webhook was
unregistered and Telegram could deliver nothing (ent#1189, B18).
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import routers.telegram as tg


class _Resp:
    def json(self):
        return {"ok": True, "result": {"username": "probe_bot"}}


class _Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        return _Resp()


def _verify(binding, public_url, register_error):
    fake_db = MagicMock()
    fake_db.get_telegram_bot_token.return_value = "tok"
    fake_db.get_telegram_binding.return_value = binding
    register = AsyncMock(return_value=register_error)
    settings = SimpleNamespace(get_setting=lambda k, d="": public_url)
    with patch.object(tg, "db", fake_db), \
         patch.object(tg.httpx, "AsyncClient", lambda **kw: _Client()), \
         patch("adapters.transports.telegram_webhook.set_webhook", register), \
         patch("services.settings_service.settings_service", settings):
        out = asyncio.run(tg.test_telegram_bot("probe", SimpleNamespace(chat_id=None, message="x")))
    return out, register


def test_registered_webhook_is_operational():
    out, register = _verify({"webhook_url": "https://x/api/telegram/webhook/s"}, "https://x", None)
    assert out["ok"] is True and out["webhook_registered"] is True
    register.assert_not_awaited()


def test_missing_webhook_without_public_url_is_not_operational():
    out, register = _verify({"webhook_url": None}, "", None)
    assert out["ok"] is False and out["webhook_registered"] is False
    assert out["webhook_error"] == "no Public URL is set in Settings"
    assert "operational" not in out["message"]
    register.assert_not_awaited()


def test_missing_webhook_with_public_url_registers():
    out, register = _verify({"webhook_url": None}, "https://x", None)
    register.assert_awaited_once_with("probe", "https://x")
    assert out["ok"] is True and out["webhook_error"] is None


def test_rejected_registration_surfaces_telegram_reason():
    reason = "Telegram rejected https://x: Bad Request: bad webhook: IP address is reserved"
    out, _ = _verify({"webhook_url": None}, "https://x", reason)
    assert out["ok"] is False
    assert out["webhook_error"] == reason
    assert reason in out["message"]
