"""trinity#3894 — a platform account with no email is told how to reach its
agents in the Workspace, by a code the Workspace renders as that remedy.

The Workspace identifies a person by email (rosters, messages, shares), so
`portal_auth.get_portal_identity` refuses a platform account without one — the
built-in `admin` on a default install. The refusal stays a 403 with a string
`detail` (every Workspace caller reads it as text), now naming the fix, and it
carries `X-Trinity-Error-Code: account_has_no_email`, the convention
`utils/skillGate.js` already reads. The Workspace's side is pinned in
src/frontend/tests/unit/workspaceAccountHasNoEmail.mount.spec.js.

Through the real route and the real `get_current_user`, with a real JWT.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import db
from db_models import UserCreate
from dependencies import create_access_token

pytestmark = pytest.mark.unit


@pytest.fixture
def client():
    from client_portal import router as portal_router
    app = FastAPI()
    app.include_router(portal_router.router)
    return TestClient(app)


def _user(email):
    name = f"admin-{uuid.uuid4().hex[:8]}"
    db.create_user(UserCreate(username=name, email=email, role="admin", password="x"))
    if email is None:  # create_user falls back to the username; the built-in admin has none
        from sqlalchemy import update
        from db.engine import get_engine
        from db.tables import users
        with get_engine().begin() as conn:
            conn.execute(update(users).where(users.c.username == name).values(email=None))
    return name


def test_an_account_without_an_email_is_refused_with_the_code_and_the_remedy(client):
    token = create_access_token({"sub": _user(None)})
    resp = client.get("/api/enterprise/client-portal/my-agents", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403, resp.text
    assert resp.headers.get("x-trinity-error-code") == "account_has_no_email"
    detail = resp.json()["detail"]
    assert isinstance(detail, str), "callers read detail as text"
    assert "no email address" in detail and "Settings → General" in detail


def test_an_account_with_an_email_reaches_its_roster(client):
    name = _user(f"{uuid.uuid4().hex[:6]}@example.com")
    db.register_agent_owner(f"owned-{name}", name)
    token = create_access_token({"sub": name})
    resp = client.get("/api/enterprise/client-portal/my-agents", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert "x-trinity-error-code" not in resp.headers
    assert f"owned-{name}" in [a["name"] for a in resp.json()["agents"]]
