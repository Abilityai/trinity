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


def test_normalise_passes_a_malformed_authority_through_unchanged():
    """`urlsplit` raises on an unterminated IPv6 bracket. The normaliser runs
    BEFORE `classify_advertised_url` on the save path, so it must hand the
    input through (not raise) and leave the refusal to the classifier."""
    assert ss.normalize_public_url("https://[::1") == "https://[::1"


def test_save_refuses_a_malformed_authority_with_422_not_500(monkeypatch):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _put(monkeypatch, "https://[::1")
    assert exc.value.status_code == 422


@pytest.mark.parametrize("row, env", [("https://host.example.com.", ""), (None, "https://host.example.com./")])
def test_resolver_strips_a_dot_already_stored_or_in_env(monkeypatch, row, env):
    svc = ss.SettingsService()
    monkeypatch.setattr(svc, "get_setting", lambda key, default=None: row)
    monkeypatch.setenv("PUBLIC_CHAT_URL", env)
    assert svc.get_public_chat_url() == "https://host.example.com"


def test_row_accessor_normalises_without_env_fallback(monkeypatch):
    svc = ss.SettingsService()
    monkeypatch.setattr(svc, "get_setting", lambda key, default=None: "https://host.example.com./")
    assert svc.get_public_chat_url_setting() == "https://host.example.com"
    monkeypatch.setattr(svc, "get_setting", lambda key, default=None: default)
    monkeypatch.setenv("PUBLIC_CHAT_URL", "https://env.example.com")
    assert svc.get_public_chat_url_setting() == ""


def test_no_webhook_registrar_reads_the_raw_row():
    """Telegram startup/connect, WhatsApp and VoIP register webhooks from the
    stored row. They go through `get_public_chat_url_setting()` so a dotted row
    stored before the save path normalised can never be registered."""
    import re
    from pathlib import Path

    backend = Path(__file__).resolve().parents[2] / "src" / "backend"
    raw = re.compile(r"""\.get_setting\(\s*["']public_chat_url["']""")
    allowed = {backend / "services" / "settings_service.py", backend / "client_portal" / "service.py"}
    hits = [
        f"{p.relative_to(backend)}:{n}"
        for p in backend.rglob("*.py")
        if p not in allowed and "enterprise" not in p.parts
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if raw.search(line)
    ]
    assert hits == [], f"raw public_chat_url reads: {hits}"
