"""#2996 — an agent's own schedule tools keep working.

Autonomy became person-only because it is the grant that decides whether cron
fires unattended. The schedule routes behind the MCP `create_schedule` /
`enable_schedule` / `disable_schedule` tools are the agent's own *use*, bounded
by that grant, and must stay reachable with the agent's own key. This pins that
the human-only rule did not spread to them.

The request goes through the REAL `get_current_user` with the agent's key row
seeded in the real schema (`tests/db_harness.py`), so the `scope` resolution and
the entry-point fences are the production ones. Each call asserts the stored
state moved, not just the status code.

Related: docs/memory/requirements/auth.md §2.8, `AGENT_CALLABLE` in
tests/unit/_route_census.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from db_harness import db_backend  # noqa: E402,F401

import dependencies  # noqa: E402
import routers.schedules as _SCHED  # noqa: E402
from database import db  # noqa: E402
from db_models import UserCreate  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "sch2996-owner"
AGENT = "sch2996-agent"


@pytest.fixture
def client(db_backend, monkeypatch):
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    db.create_user(
        UserCreate(username=OWNER, role="user", email="sch2996-owner@example.com")
    )
    db.register_agent_owner(AGENT, OWNER)
    app = FastAPI()
    app.include_router(_SCHED.router)
    return TestClient(app)


def _agent_key() -> dict:
    key = db.create_agent_mcp_api_key(AGENT, OWNER).api_key
    return {"Authorization": f"Bearer {key}"}


def test_the_agent_key_creates_enables_and_disables_its_own_schedule(client):
    headers = _agent_key()
    base = f"/api/agents/{AGENT}/schedules"

    res = client.post(
        base,
        json={"name": "nightly", "cron_expression": "0 3 * * *", "message": "run"},
        headers=headers,
    )
    assert res.status_code == 201, res.text
    schedule_id = res.json()["id"]
    assert db.get_schedule(schedule_id).agent_name == AGENT

    res = client.post(f"{base}/{schedule_id}/disable", headers=headers)
    assert res.status_code == 200, res.text
    assert db.get_schedule(schedule_id).enabled is False

    res = client.post(f"{base}/{schedule_id}/enable", headers=headers)
    assert res.status_code == 200, res.text
    assert db.get_schedule(schedule_id).enabled is True
