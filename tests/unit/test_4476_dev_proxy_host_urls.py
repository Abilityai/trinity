"""The URLs the backend hands a browser name the host the browser used
(abilityai/trinity#4476).

Found in the v1.0.5 release testing: on the default local install, Settings →
MCP Keys advertised `http://backend:8080/mcp`, and a schedule's webhook URL read
`http://backend:8000/api/webhooks/<token>`. The backend builds both from the
request's Host (`routers/settings/mcp_url.py::_get_default_mcp_url`,
`utils/public_url.py::public_base_url`), as it must behind the production nginx,
which sends `Host $host`. The dev server's `/api/` proxy rule replaced the
browser's Host with the container name; it now keeps it, and
`src/frontend/tests/unit/viteApiProxyHost.spec.js` sends requests through the
real rule to prove it. This file pins the other half: given the Host that rule
delivers, the real routes answer with an address the browser's machine can use.

Every request is a real one (TestClient → Starlette). The code reads the host
from two places — `request.url.netloc`, which Starlette rebuilds from the Host
header, and the raw `host` header — and a stand-in that stores one of them
cannot show the two disagreeing.

Also pinned: an IPv6-literal Host. Splitting the header on ":" turned `[::1]`
into `http://[:8080/mcp`; a browser on such an origin reaches this code through
the dev proxy now that the proxy keeps the Host.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from db_harness import db_backend  # noqa: E402,F401 — the fixture, used by name

import dependencies  # noqa: E402
import routers.schedules as schedules_router  # noqa: E402
import routers.settings as settings_router  # noqa: E402  the composed router
from database import db  # noqa: E402
from db_models import ScheduleCreate, UserCreate  # noqa: E402
from models import User  # noqa: E402

pytestmark = pytest.mark.unit

# The two routes the MCP Keys page and the API Keys page read, and the field
# each one carries the auto-detected address in.
MCP_ROUTES = [
    ("/api/settings/mcp-external-access", "advertised_url"),
    ("/api/settings/mcp-url", "default_url"),
]


# ---------------------------------------------------------------------------
# The advertised MCP URL
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings_client(monkeypatch):
    """The real composed settings router, with an empty system_settings table
    (so no `mcp_external_url` override) and no MCP_BIND_ADDRESS — the install
    whose snippet showed `backend:8080`."""
    rows: dict = {}
    monkeypatch.setattr(db, "get_setting_value", lambda key, default=None: rows.get(key, default))
    monkeypatch.delenv("MCP_BIND_ADDRESS", raising=False)
    monkeypatch.delenv("MCP_EXTERNAL_ACCESS", raising=False)
    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[dependencies.get_current_user] = lambda: User(
        id=1, username="admin", role="admin"
    )
    return TestClient(app)


def _advertised(client, path, field, host):
    res = client.get(path, headers={"host": host})
    assert res.status_code == 200, res.text
    return res.json()[field]


@pytest.mark.parametrize("path, field", MCP_ROUTES)
@pytest.mark.parametrize(
    "host, expected",
    [
        # The page at http://localhost: the default local install.
        ("localhost", "http://localhost:8080/mcp"),
        # Vite run on the host serves the page on its own port.
        ("localhost:5173", "http://localhost:8080/mcp"),
        # The dev install opened from another machine.
        ("203.0.113.20", "http://203.0.113.20:8080/mcp"),
        ("trinity.test:8081", "http://trinity.test:8080/mcp"),
    ],
)
def test_the_delivered_host_is_the_advertised_host(settings_client, path, field, host, expected):
    assert _advertised(settings_client, path, field, host) == expected


@pytest.mark.parametrize("path, field", MCP_ROUTES)
@pytest.mark.parametrize(
    "host, expected",
    [
        ("[::1]", "http://localhost:8080/mcp"),
        ("[::1]:80", "http://localhost:8080/mcp"),
        ("[2001:db8::7]", "http://[2001:db8::7]:8080/mcp"),
        ("[2001:db8::7]:5173", "http://[2001:db8::7]:8080/mcp"),
    ],
)
def test_an_ipv6_literal_host_keeps_its_brackets(settings_client, path, field, host, expected):
    assert _advertised(settings_client, path, field, host) == expected


@pytest.mark.parametrize("path, field", MCP_ROUTES)
def test_the_backend_names_whatever_host_it_is_given(settings_client, path, field):
    """`backend:8000` is what the old rule delivered. The backend is right to
    trust the Host, so the fix belongs in the proxy, not in a mapping of
    container names here."""
    assert _advertised(settings_client, path, field, "backend:8000") == "http://backend:8080/mcp"


@pytest.mark.parametrize("path, field", MCP_ROUTES)
def test_a_loopback_bind_still_wins_over_the_delivered_host(settings_client, monkeypatch, path, field):
    """A fresh `.env.example` install publishes the raw port on loopback, and
    advertises the loopback URL whatever host the request carried."""
    monkeypatch.setenv("MCP_BIND_ADDRESS", "127.0.0.1")
    assert _advertised(settings_client, path, field, "localhost") == "http://127.0.0.1:8080/mcp"


# ---------------------------------------------------------------------------
# A schedule's webhook URL
# ---------------------------------------------------------------------------

OWNER = "owner-4476"
AGENT = "agent-4476"


@pytest.fixture
def webhook(db_backend, monkeypatch):  # noqa: F811 — the db_harness fixture
    """The real schedules router over the real schema: an admin who owns an
    agent with one schedule."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    db.create_user(UserCreate(username=OWNER, role="admin", email="owner-4476@example.com"))
    db.register_agent_owner(AGENT, OWNER)
    owner_id = db.get_user_by_username(OWNER)["id"]
    schedule = db.create_schedule(
        AGENT,
        OWNER,
        ScheduleCreate(name="nightly", cron_expression="0 3 * * *", message="run", enabled=False),
    )
    assert schedule is not None

    app = FastAPI()
    app.include_router(schedules_router.router)
    app.dependency_overrides[dependencies.get_current_user] = lambda: User(
        id=owner_id, username=OWNER, email="owner-4476@example.com", role="admin"
    )
    yield SimpleNamespace(
        client=TestClient(app),
        path=f"/api/agents/{AGENT}/schedules/{schedule.id}/webhook",
        token=lambda: db.get_webhook_status(schedule.id)["webhook_token"],
    )
    app.dependency_overrides.clear()


WEBHOOK_ORIGINS = [
    ("localhost", "http://localhost"),
    # Vite run on the host: the URL comes back on the page's own port, where
    # the same proxy rule carries it to the backend.
    ("localhost:5173", "http://localhost:5173"),
    # What the old rule delivered: unreachable from the browser's machine.
    ("backend:8000", "http://backend:8000"),
]


@pytest.mark.parametrize("host, origin", WEBHOOK_ORIGINS)
def test_minting_a_webhook_names_the_delivered_host(webhook, host, origin):
    res = webhook.client.post(webhook.path, headers={"host": host})
    assert res.status_code == 200, res.text
    assert res.json()["webhook_url"] == f"{origin}/api/webhooks/{webhook.token()}"


@pytest.mark.parametrize("host, origin", WEBHOOK_ORIGINS)
def test_reading_a_webhook_names_the_delivered_host(webhook, host, origin):
    assert webhook.client.post(webhook.path, headers={"host": "localhost"}).status_code == 200
    res = webhook.client.get(webhook.path, headers={"host": host})
    assert res.status_code == 200, res.text
    assert res.json()["webhook_url"] == f"{origin}/api/webhooks/{webhook.token()}"
