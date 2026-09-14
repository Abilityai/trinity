"""`GET /api/public/tls-allowed` reached through FastAPI, not sliced out (#2380).

`test_2380_provision_single_source.py` proves the DECISION — exact hostname,
fail closed — by exec-slicing the function out of `routers/public.py`, because
importing that module pulls the whole backend graph. That is the right shape for
the logic, and it cannot see any of this:

- the route is registered on the router at the path Caddy is configured to call
  (`ask http://127.0.0.1:8000/api/public/tls-allowed`, written into the
  Caddyfile by `start.sh --provision`) — a renamed path or a changed prefix
  leaves the slice passing while every certificate request is refused;
- it takes `domain` as a QUERY parameter, which is how Caddy sends it;
- it carries no auth dependency, so Caddy — which holds no Trinity credential
  and calls this inside a TLS handshake — is not answered with a 401;
- a refusal is the 404 Caddy documents as "not authorised", with no server
  error escaping as a 500.

So this file mounts the real router in a throwaway app and calls the real URL.
"""
from __future__ import annotations

import os
import tempfile

import pytest

# Backend config raises at import without these; keep DB side effects in a temp
# file so importing the router's dependencies cannot touch a real /data volume.
os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault(
    "TRINITY_DB_PATH", os.path.join(tempfile.gettempdir(), "trinity_2380_ask_test.db")
)

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from routers import public  # noqa: E402

pytestmark = pytest.mark.unit

_PATH = "/api/public/tls-allowed"  # the path in the provisioned Caddyfile


@pytest.fixture
def client(monkeypatch):
    def _configured(url):
        monkeypatch.setattr(public.settings_service, "get_public_chat_url", lambda: url)

    app = FastAPI()
    app.include_router(public.router)
    c = TestClient(app, raise_server_exceptions=False)
    c.configure = _configured
    return c


def test_the_configured_host_is_authorised(client) -> None:
    client.configure("https://trinity.example.com")
    r = client.get(_PATH, params={"domain": "trinity.example.com"})
    assert r.status_code == 200, r.text
    assert r.json()["domain"] == "trinity.example.com"


@pytest.mark.parametrize(
    "requested",
    [
        "evil-trinity.example.com",  # prefix games the substring check would pass
        "trinity.example.com.evil",
        "example.com",  # the parent domain is not the configured host
        "",  # Caddy calling with nothing
    ],
)
def test_any_other_name_is_refused_with_404(client, requested: str) -> None:
    client.configure("https://trinity.example.com")
    r = client.get(_PATH, params={"domain": requested})
    assert r.status_code == 404, f"{requested!r} was authorised: {r.text}"


def test_no_public_url_authorises_nothing(client) -> None:
    """Before an admin saves one, the instance must issue for no name at all."""
    client.configure("")
    r = client.get(_PATH, params={"domain": "trinity.example.com"})
    assert r.status_code == 404, r.text


def test_a_failing_settings_read_refuses_rather_than_500s(client, monkeypatch) -> None:
    """Fails closed, and stays a refusal Caddy understands — a 500 inside the
    handshake is both an authorisation it cannot read and a slower one."""
    def _boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(public.settings_service, "get_public_chat_url", _boom)
    r = client.get(_PATH, params={"domain": "trinity.example.com"})
    assert r.status_code == 404, r.text


def test_the_route_needs_no_credential(client) -> None:
    """Caddy holds none. A 401/403 here would break certificate issuance for
    every domain, and the breakage would only show up in a live handshake."""
    client.configure("https://trinity.example.com")
    r = client.get(_PATH, params={"domain": "trinity.example.com"})
    assert r.status_code not in (401, 403), r.text
    route = next(r for r in public.router.routes if r.path.endswith("/tls-allowed"))
    assert not getattr(route, "dependencies", []), "the ask gate gained a dependency"
