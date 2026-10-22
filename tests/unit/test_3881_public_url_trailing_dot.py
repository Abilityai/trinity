"""#3881 — a Public URL whose host ends in a dot (`https://host.example.com.`)
is stored and handed out without the dot. An absolute name is the same name,
but the TLS certificate is for the name without the dot, so Telegram's webhook
check failed and no message was ever delivered.

Fixed at both ends: the save path stores the normalised value, and the
canonical resolver `get_public_chat_url()` normalises whatever it reads (a row
written before this fix, or the `PUBLIC_CHAT_URL` env), so every consumer —
bot webhooks, public links, WhatsApp, paid chat — gets the clean host.
"""
import asyncio
import types

import pytest

from services import settings_service as ss

pytestmark = pytest.mark.unit

CASES = [
    ("https://host.example.com.", "https://host.example.com"),
    ("https://host.example.com./", "https://host.example.com"),
    ("https://host.example.com.:8443", "https://host.example.com:8443"),
    ("https://host.example.com./chat", "https://host.example.com/chat"),
    (" https://host.example.com. ", "https://host.example.com"),
    # unchanged
    ("https://host.example.com", "https://host.example.com"),
    ("https://host.example.com/", "https://host.example.com"),
    ("http://trinity.example.com:8080", "http://trinity.example.com:8080"),
    ("http://10.0.0.5", "http://10.0.0.5"),
    ("", ""),
]


@pytest.mark.parametrize("raw, expected", CASES)
def test_normalise(raw, expected):
    assert ss.normalize_public_url(raw) == expected


def _put(monkeypatch, value):
    from routers.settings import generic as m

    written, repointed = {}, []

    async def _noop_audit(**_kwargs):
        return None

    async def _backfill(url):
        repointed.append(url)

    monkeypatch.setattr(m, "assert_admin", lambda *_a, **_k: None)
    monkeypatch.setattr(m.db, "set_setting", lambda k, v: written.setdefault(k, v) or {"key": k, "value": v, "description": None, "updated_at": None})
    monkeypatch.setattr(m.platform_audit_service, "log", _noop_audit)
    monkeypatch.setattr(m, "_backfill_telegram_webhooks", _backfill)
    body = types.SimpleNamespace(value=value, description=None)
    request = types.SimpleNamespace(
        client=types.SimpleNamespace(host="127.0.0.1"),
        scope={"path": "/api/settings/public_chat_url"},
        state=types.SimpleNamespace(),
    )
    user = types.SimpleNamespace(id=1, username="admin", role="admin", mcp_scope=None, agent_name=None)
    asyncio.run(m.update_setting("public_chat_url", body, request, user))
    return written, repointed


@pytest.mark.parametrize("raw", ["https://host.example.com.", "https://host.example.com./"])
def test_save_stores_and_repoints_without_the_dot(monkeypatch, raw):
    written, repointed = _put(monkeypatch, raw)
    assert written["public_chat_url"] == "https://host.example.com"
    assert repointed == ["https://host.example.com"]


@pytest.mark.parametrize("row, env", [("https://host.example.com.", ""), (None, "https://host.example.com./")])
def test_resolver_strips_a_dot_already_stored_or_in_env(monkeypatch, row, env):
    svc = ss.SettingsService()
    monkeypatch.setattr(svc, "get_setting", lambda key, default=None: row)
    monkeypatch.setenv("PUBLIC_CHAT_URL", env)
    assert svc.get_public_chat_url() == "https://host.example.com"
