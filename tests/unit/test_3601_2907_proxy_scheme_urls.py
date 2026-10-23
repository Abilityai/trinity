"""Behind an HTTPS proxy, the webhook URL (abilityai/trinity#3601) and the
auto-detected MCP URL (abilityai/trinity#2907) start with https://.

Both built their origin without looking at `X-Forwarded-Proto`, so a TLS install
advertised http://. They now take the origin from `utils.public_url.public_base_url`
(#3215), which upgrades http to https on the forwarded header and never
downgrades.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "backend"))

import routers.schedules as schedules  # noqa: E402
import routers.settings.mcp_url as mcp_url  # noqa: E402


def _request(url="http://trinity.test/api/x", **headers):
    """A stand-in for a Starlette Request: `.url` and `.headers` only."""
    scheme, rest = url.split("://", 1)
    netloc = rest.split("/", 1)[0]
    return SimpleNamespace(
        url=SimpleNamespace(scheme=scheme, netloc=netloc),
        headers={"host": netloc,
                 **{k.replace("_", "-").lower(): v for k, v in headers.items()}},
    )


# ---------------------------------------------------------------------------
# #3601 — webhook URL
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("headers, expected", [
    ({"x_forwarded_proto": "https"}, "https://trinity.test/api/webhooks/tok"),
    ({"x_forwarded_proto": "https, http"}, "https://trinity.test/api/webhooks/tok"),
    ({"x_forwarded_proto": "http"}, "http://trinity.test/api/webhooks/tok"),
    ({}, "http://trinity.test/api/webhooks/tok"),
])
def test_webhook_url_follows_forwarded_scheme(headers, expected):
    assert schedules._build_webhook_url(_request(**headers), "tok") == expected


def test_webhook_url_never_downgrades_https():
    req = _request("https://trinity.test/api/x", x_forwarded_proto="http")
    assert schedules._build_webhook_url(req, "tok") == "https://trinity.test/api/webhooks/tok"


def test_every_webhook_route_passes_the_request():
    """A route that hands over `request.base_url` (or any str) gets no upgrade
    and, from `public_base_url`, no origin at all."""
    tree = ast.parse(Path(schedules.__file__).read_text())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_build_webhook_url"]
    assert len(calls) == 4
    for call in calls:
        assert isinstance(call.args[0], ast.Name) and call.args[0].id == "request", ast.unparse(call)


def test_webhook_url_is_none_without_a_host():
    req = SimpleNamespace(url=SimpleNamespace(scheme="http", netloc=""), headers={})
    assert schedules._build_webhook_url(req, "tok") is None


# ---------------------------------------------------------------------------
# #2907 — auto-detected MCP URL
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url, headers, expected", [
    # Proxy serves /mcp on the same origin, so no :8080.
    ("http://trinity.test/api/x", {"x_forwarded_proto": "https"}, "https://trinity.test/mcp"),
    ("http://trinity.test/api/x", {"x_forwarded_proto": "http"}, "http://trinity.test:8080/mcp"),
    ("http://trinity.test/api/x", {}, "http://trinity.test:8080/mcp"),
    ("http://localhost/api/x", {}, "http://localhost:8080/mcp"),
    ("http://127.0.0.1:8000/api/x", {}, "http://localhost:8080/mcp"),
])
def test_default_mcp_url(url, headers, expected):
    assert mcp_url._get_default_mcp_url(_request(url, **headers)) == expected


def test_stored_override_wins(monkeypatch):
    monkeypatch.setattr(mcp_url.db, "get_setting_value",
                        lambda key: "http://custom.example/mcp")
    req = _request(x_forwarded_proto="https")
    assert mcp_url.resolve_mcp_url(req) == "http://custom.example/mcp"


def test_no_override_uses_detected_default(monkeypatch):
    monkeypatch.setattr(mcp_url.db, "get_setting_value", lambda key: None)
    req = _request(x_forwarded_proto="https")
    assert mcp_url.resolve_mcp_url(req) == "https://trinity.test/mcp"
