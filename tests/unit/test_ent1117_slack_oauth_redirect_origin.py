"""The Slack OAuth callback always redirects to the configured frontend origin.

`GET /api/public/slack/oauth/callback` is unauthenticated (Slack's servers call
it), and it answers every outcome with a redirect built by
`SlackService.get_oauth_callback_redirect`:

    f"{base_url}/agents/{agent_name}?tab=sharing&slack=..."
    f"{base_url}/settings?slack=...&reason={error}"

with `base_url = FRONTEND_URL or get_public_chat_url()` (server configuration).
Caller-influenced text (the `error` query parameter, the token-exchange error,
and `agent_name` / `source` from the signed `state`) only ever lands AFTER the
literal "/" that ends the authority, so it can change the path and query but
never the scheme or host. Starlette's `RedirectResponse` percent-quotes the
Location, so CR/LF cannot add a header.

`test_3563_slack_oauth_redirect_base.py` pins WHICH base is chosen. This file
pins that no caller value can move the redirect off that base, at every one of
the route's five redirects (the `error` parameter, a failed token exchange,
platform success, agent success, and the storage-failure branch).

To make the check strictly harder than reality, the signed `state` is not
required: the state decoder is replaced so the test controls every state key
(`agent_name`, `source`, and any key the route might read in future, which
returns the hostile value), and the token exchange returns test-controlled
error text.

Patches go through the route function's own `__globals__` (and the redirect
helper's), so the test exercises whichever module object the route actually
runs in, regardless of import order elsewhere in the session.

abilityai/trinity-enterprise#1117
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.slack as slack_router  # noqa: E402

pytestmark = pytest.mark.unit

ORIGIN_HOST = "trinity.example.com"
ORIGIN = f"https://{ORIGIN_HOST}"
CALLBACK_PATH = "/api/public/slack/oauth/callback"

# Values a hostile caller would try in order to move a redirect off-origin.
HOSTILE = [
    "//evil.com",
    "/\\evil.com",
    "\\\\evil.com",
    "@evil.com",
    ".evil.com",
    ":@evil.com",
    "#@evil.com",
    "?@evil.com",
    "https://evil.com",
    "http:evil.com",
    "%2F%2Fevil.com",
    "%5C%5Cevil.com",
    "\r\nLocation: https://evil.com",
    "x\r\n\r\n<script>",
    "../../../@evil.com",
    "javascript:alert(1)",
    "。evil.com",
    "\x00@evil.com",
]

# Every redirect the route can produce. For each: which query the caller sends
# (`error` parameter, or a code + state), the `source` carried in the state
# (None = the hostile value itself, which the route treats as an agent install),
# whether the token exchange succeeds, whether the storage write fails, the
# path the base is joined to, and the marker that proves the branch was reached.
SITES = {
    "error_param": ("error", None, True, False, "agents", "slack=error&reason="),
    "exchange_failure_agent": ("code", None, False, False, "agents", "slack=error&reason="),
    "exchange_failure_platform": ("code", "platform", False, False, "settings", "slack=error&reason="),
    "platform_success": ("code", "platform", True, False, "settings", "slack=installed"),
    "agent_success": ("code", None, True, False, "agents", "slack=connected"),
    "storage_failure_agent": ("code", None, True, True, "agents", "slack=error&reason=database_error"),
    "storage_failure_platform": ("code", "platform", True, True, "settings", "slack=error&reason=database_error"),
}


class _AnyKey(dict):
    """A decoded state where every key the test did not set holds `default`,
    and every key reads as present: the route cannot read a field the caller
    does not control."""

    def __init__(self, default, **given):
        super().__init__(**given)
        self._default = default

    def __missing__(self, key):
        return self._default

    def __contains__(self, key):
        return True

    def get(self, key, default=None):
        return self[key]


class _Db:
    """Stands in for the storage the success branches write to."""

    def __init__(self) -> None:
        self.fail = False

    def create_slack_workspace(self, **_kw):
        if self.fail:
            raise RuntimeError("storage unavailable")

    def create_slack_connection(self, **_kw):
        return None

    def bind_slack_channel_to_agent(self, **_kw):
        return None


@pytest.fixture
def callback(monkeypatch):
    """Calls the real callback route, with its collaborators replaced."""
    route = next(
        r for r in slack_router.public_router.routes
        if getattr(r, "path", "") == CALLBACK_PATH
    )
    route_globals = route.endpoint.__globals__
    svc = route_globals["slack_service"]
    redirect_globals = type(svc).get_oauth_callback_redirect.__globals__

    monkeypatch.setitem(redirect_globals, "FRONTEND_URL", ORIGIN)

    def _public_url_must_not_be_read():
        raise AssertionError("FRONTEND_URL is set, so the Public URL must not be consulted")

    monkeypatch.setitem(redirect_globals, "get_public_chat_url", _public_url_must_not_be_read)

    controls = {"state": {}, "exchange": (False, {})}
    db = _Db()

    async def _exchange(_code):
        return controls["exchange"]

    async def _create_channel(_token, _name):
        return (False, None, "not created in this test")

    async def _audit(**_kw):
        return None

    monkeypatch.setattr(svc, "decode_oauth_state", lambda _s: (True, controls["state"]))
    monkeypatch.setattr(svc, "exchange_oauth_code", _exchange)
    monkeypatch.setattr(svc, "create_channel", _create_channel)
    monkeypatch.setattr(route_globals["platform_audit_service"], "log", _audit)
    monkeypatch.setitem(route_globals, "db", db)

    app = FastAPI()
    app.include_router(slack_router.public_router)
    client = TestClient(app)

    def call(site: str, value: str):
        query, source, exchange_ok, storage_fails, _path, _marker = SITES[site]
        controls["state"] = _AnyKey(
            value,
            link_id="l1",
            agent_name=value,
            user_id=1,
            source=value if source is None else source,
        )
        controls["exchange"] = (
            (True, {"team_id": "T1", "team_name": "team", "access_token": "test-token"})
            if exchange_ok
            else (False, {"error": value})
        )
        db.fail = storage_fails
        params = {"error": value} if query == "error" else {"code": "c", "state": "s"}
        try:
            return client.get(CALLBACK_PATH, params=params, follow_redirects=False)
        except httpx.RemoteProtocolError as exc:
            # The client refuses a Location it cannot parse (a raw CR/LF, a
            # scheme-relative "//host"): that is an off-origin redirect too.
            raise AssertionError(f"{site}: the route emitted an unparseable Location: {exc}") from exc

    return call


def _browser_host(location: str) -> str:
    """The host a WHATWG browser would navigate to: it treats a backslash like
    a slash and drops any userinfo before the last '@' in the authority."""
    rest = location.split("://", 1)[1]
    for i, ch in enumerate(rest):
        if ch in "/\\?#":
            rest = rest[:i]
            break
    return rest.rsplit("@", 1)[-1].split(":")[0].lower()


@pytest.mark.parametrize("site", sorted(SITES))
@pytest.mark.parametrize("value", HOSTILE, ids=[repr(v) for v in HOSTILE])
def test_every_callback_redirect_stays_on_the_configured_origin(callback, site, value):
    response = callback(site, value)
    location = response.headers.get("location", "")
    *_, path_kind, marker = SITES[site]

    assert response.status_code == 307, (response.status_code, location)
    # Exactly one Location and nothing else: no header was smuggled in.
    assert sorted(k.decode().lower() for k, _ in response.headers.raw) == [
        "content-length",
        "location",
    ], response.headers.raw
    assert "\r" not in location and "\n" not in location, repr(location)
    # The origin is the configured one, under both Python's and a browser's parse.
    assert location.startswith(f"{ORIGIN}/{path_kind}"), location
    parts = urlsplit(location)
    assert (parts.scheme, parts.hostname, parts.port) == ("https", ORIGIN_HOST, None), location
    assert _browser_host(location) == ORIGIN_HOST, location
    # And the branch under test really is the one that produced it.
    assert marker in location, (site, location)


def test_the_hostile_set_reaches_every_redirect_branch(callback):
    """Guards the guard: each site key maps to a distinct branch of the route,
    so the parametrized test above is not exercising one branch seven times."""
    seen = set()
    for site in SITES:
        location = callback(site, "probe-value").headers["location"]
        seen.add(location.replace("probe-value", "<v>"))
    assert seen == {
        f"{ORIGIN}/agents/unknown?tab=sharing&slack=error&reason=<v>",
        f"{ORIGIN}/agents/<v>?tab=sharing&slack=error&reason=<v>",
        f"{ORIGIN}/settings?slack=error&reason=<v>",
        f"{ORIGIN}/settings?slack=installed",
        f"{ORIGIN}/agents/<v>?tab=sharing&slack=connected",
        f"{ORIGIN}/agents/<v>?tab=sharing&slack=error&reason=database_error",
        f"{ORIGIN}/settings?slack=error&reason=database_error",
    }
