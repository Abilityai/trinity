"""
Slack OAuth callback redirect base URL.

With ``FRONTEND_URL`` empty (the ``.env.example`` default) the redirect used to
fall straight to ``http://localhost``. It now falls back to the Public URL
setting the OAuth redirect URI is built from; ``FRONTEND_URL`` keeps precedence.
"""

import sys
from pathlib import Path

import pytest

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from services import slack_service as mod  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def svc():
    return mod.SlackService()


def _set(monkeypatch, frontend, public):
    monkeypatch.setattr(mod, "FRONTEND_URL", frontend)
    monkeypatch.setattr(mod, "get_public_chat_url", lambda: public)


def test_empty_frontend_url_uses_public_url(monkeypatch, svc):
    _set(monkeypatch, "", "https://trinity.example.com")
    assert (
        svc.get_oauth_callback_redirect("a", source="platform")
        == "https://trinity.example.com/settings?slack=installed"
    )
    assert (
        svc.get_oauth_callback_redirect("a")
        == "https://trinity.example.com/agents/a?tab=sharing&slack=connected"
    )
    assert svc.get_oauth_callback_redirect(
        "a", success=False, error="x", source="platform"
    ).startswith("https://trinity.example.com/settings?slack=error")
    assert svc.get_oauth_callback_redirect(
        "a", success=False, error="x"
    ).startswith("https://trinity.example.com/agents/a?")


def test_frontend_url_takes_precedence(monkeypatch, svc):
    _set(monkeypatch, "https://ui.example.com", "https://trinity.example.com")
    assert svc.get_oauth_callback_redirect("a", source="platform").startswith(
        "https://ui.example.com/"
    )


def test_localhost_only_when_nothing_configured(monkeypatch, svc):
    _set(monkeypatch, "", "")
    assert svc.get_oauth_callback_redirect("a", source="platform").startswith(
        "http://localhost/"
    )
