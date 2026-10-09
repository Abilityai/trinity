"""trinity-enterprise#837 — an `authorized` canvas link recognises a Workspace session.

An `authorized` share link points at a canvas the viewer could already see, and
the page offers a sign-in when it cannot tell who the viewer is. Before #837,
sharing an agent wrote a login-whitelist row, so "Sign in" at `/login` worked
for the people the agent was shared with. #837 stopped that write: someone
shared after it signs in to the Workspace with an emailed code and never
receives a `/login` code, and the canvas route read only a platform JWT, so the
link dead-ended for exactly the audience it was made for.

Pinned here:
  * the service: a Workspace viewer sees what the Workspace shows it — an agent
    on its roster AND a canvas addressed to the roster; never an operator
    canvas, never another roster's agent (real database);
  * the route: with no platform credential it reads a Workspace session from the
    bearer token; an invalid token stays anonymous; a blocked client is refused;
    a platform JWT still decides as before.
"""
from __future__ import annotations

import pytest

from models import User

pytestmark = pytest.mark.unit


@pytest.fixture()
def share_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-share-837.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        metadata as m, access_requests, agent_canvases, agent_canvas_shares,
        agent_ownership, agent_sharing, users, schedule_executions,
    )
    # Every share here is created with an explicit scope, so the metadata shape
    # is enough (ent#554 needs the real DDL only to prove a column default).
    m.create_all(get_engine(), tables=[
        access_requests, agent_canvases, agent_canvas_shares, agent_ownership,
        agent_sharing, users, schedule_executions,
    ])

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(id=1, username="owner", role="operator",
                                          email="owner@example.com",
                                          created_at="t", updated_at="t"))
        conn.execute(insert(agent_ownership).values(
            agent_name="agent-a", owner_id=1, created_at="t"))
    from database import db
    db.share_agent("agent-a", "owner", "guest@example.com")
    yield db


def _share(db, audience):
    db.upsert_agent_canvas("agent-a", f"board-{audience}", blocks=[], title="Board",
                           audience=audience)
    return db.create_canvas_share("agent-a", f"board-{audience}", scope="authorized")["token"]


def _status(token, viewer):
    from services import canvas_share_service as css
    return css.resolve(token, viewer)["status"]


def test_a_workspace_viewer_opens_a_roster_canvas_of_an_agent_shared_with_them(share_db):
    from services.canvas_share_service import ShareResolution, WorkspaceViewer
    token = _share(share_db, "roster")
    assert _status(token, WorkspaceViewer("guest@example.com")) == ShareResolution.OK


def test_a_workspace_viewer_never_opens_an_operator_canvas(share_db):
    """The Workspace never shows a client an operator-audience canvas, so the link
    must not either: the link points, it does not grant."""
    from services.canvas_share_service import ShareResolution, WorkspaceViewer
    token = _share(share_db, "operator")
    assert _status(token, WorkspaceViewer("guest@example.com")) == ShareResolution.NOT_AUTHORIZED


def test_a_workspace_viewer_without_the_agent_is_refused(share_db):
    from services.canvas_share_service import ShareResolution, WorkspaceViewer
    token = _share(share_db, "roster")
    assert _status(token, WorkspaceViewer("other@example.com")) == ShareResolution.NOT_AUTHORIZED


def test_a_platform_principal_still_decides_by_agent_access(share_db):
    from services.canvas_share_service import ShareResolution
    token = _share(share_db, "operator")
    assert _status(token, User(id=1, username="owner", role="operator")) == ShareResolution.OK


# ---------------------------------------------------------------------------
# The route reads a Workspace session from the bearer token
# ---------------------------------------------------------------------------

@pytest.fixture
def canvas_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import client_portal.portal_auth as portal_auth
    import routers.public as public
    from services import canvas_share_service as css

    seen = {}

    def fake_resolve(token, viewer):
        seen["viewer"] = viewer
        if viewer is None:
            return {"status": css.ShareResolution.SIGN_IN_REQUIRED, "canvas": None, "share": None}
        return {"status": css.ShareResolution.OK, "canvas": {"id": "c"}, "share": {"scope": "authorized"}}

    monkeypatch.setattr(public, "check_public_link_rate_limit", lambda *_a, **_k: None)
    monkeypatch.setattr(css, "resolve", fake_resolve)
    monkeypatch.setattr(css, "public_view_payload", lambda resolution: {"ok": True})
    monkeypatch.setattr(portal_auth, "decode_portal_session",
                        lambda tok: "guest@example.com" if tok == "portal-token" else None)
    monkeypatch.setattr(portal_auth, "_reject_if_blocked", lambda email: None)

    app = FastAPI()
    app.include_router(public.router)
    path = next(r.path for r in app.routes if r.path.endswith("/canvas/{token}"))
    return TestClient(app), path.replace("{token}", "share-1"), seen


def test_the_route_recognises_a_workspace_session(canvas_client):
    from services.canvas_share_service import WorkspaceViewer
    client, url, seen = canvas_client
    r = client.get(url, headers={"Authorization": "Bearer portal-token"})
    assert r.status_code == 200
    assert seen["viewer"] == WorkspaceViewer("guest@example.com")


def test_an_unknown_bearer_token_stays_anonymous(canvas_client):
    client, url, seen = canvas_client
    r = client.get(url, headers={"Authorization": "Bearer not-a-session"})
    assert r.status_code == 401
    assert seen["viewer"] is None


def test_a_blocked_workspace_client_is_refused(canvas_client, monkeypatch):
    from fastapi import HTTPException
    import client_portal.portal_auth as portal_auth

    def blocked(_email):
        raise HTTPException(status_code=403, detail="blocked")

    monkeypatch.setattr(portal_auth, "_reject_if_blocked", blocked)
    client, url, _seen = canvas_client
    r = client.get(url, headers={"Authorization": "Bearer portal-token"})
    assert r.status_code == 403
