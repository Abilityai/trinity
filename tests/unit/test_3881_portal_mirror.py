"""#3881 — the client portal's local resolver copy (`client_portal.service
._public_chat_url`) mirrors `settings_service.get_public_chat_url()` and must
normalise the same way: a dotted stored row or a dotted `PUBLIC_CHAT_URL` env
must never reach a portal link with the dot. Pinned here because reverting the
mirror hunk alone left the suite green."""
import pytest

from client_portal import service as portal

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("row, env", [("https://host.example.com.", ""), ("", "https://host.example.com./")])
def test_portal_base_url_falls_back_to_the_public_url_without_the_dot(monkeypatch, row, env):
    monkeypatch.setattr(portal.db, "get_setting", lambda key, default="": row if key == "public_chat_url" else default)
    monkeypatch.setenv("PUBLIC_CHAT_URL", env)
    assert portal.get_portal_base_url() == "https://host.example.com"
