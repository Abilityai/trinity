"""#3063 — Workspace history never carries a turn's `cost`.

`GET /client-portal/agents/{name}/history` serialised `cost` on every message to
every principal, a portal token (an external client) included. The Workspace
contract is that external clients never see costs, and every other projection
already honours it (the Work projection: "No message, response, cost or
model"). Nothing in the frontend reads a history message's cost, so it is
projected out for EVERY principal rather than split by principal.

Driven through the REAL route (`response_model=PortalHistory` is the projection
under test) against a throwaway sqlite carrying the real portal tables, with a
row written by the real writer carrying a real cost.

Execution ids are deliberately NOT asserted absent: `in_flight_execution_id`
and `last_turn_outcome.execution_id` are the client's own turn handles — the
202 dispatch response issues the same id, and the client streams, cancels and
reattaches by it. That decision is stated in the PR.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

CLIENT = "client@example.com"
AGENT = "scribe"
THREAD = "thread-3063"


@pytest.fixture()
def client_for(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-3063.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.engine import get_engine
    from db.tables import metadata, enterprise_portal_messages, enterprise_portal_sessions
    metadata.create_all(get_engine(), tables=[enterprise_portal_messages, enterprise_portal_sessions])

    from client_portal import db as pdb, service as svc
    from utils.helpers import utc_now_iso
    pdb.create_portal_session(THREAD, AGENT, CLIENT, utc_now_iso())
    pdb.add_portal_message("m-user", AGENT, CLIENT, "user", "what did it cost?", None,
                           utc_now_iso(), session_id=THREAD)
    pdb.add_portal_message("m-reply", AGENT, CLIENT, "assistant", "here you go", 0.4242,
                           utc_now_iso(), session_id=THREAD)
    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_attach_own_ratings", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "get_turn_inflight", lambda sid: None)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from client_portal import router
    from client_portal.portal_auth import PortalPrincipal, get_portal_principal

    def make(is_platform: bool):
        app = FastAPI()
        app.include_router(router.router)
        app.dependency_overrides[get_portal_principal] = lambda: PortalPrincipal(
            email=CLIENT, is_platform=is_platform)
        return TestClient(app)
    return make


def _keys(obj):
    """Every key anywhere in a JSON document."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


@pytest.mark.parametrize("is_platform", [False, True], ids=["portal-token", "platform"])
def test_history_carries_no_cost_anywhere(client_for, is_platform):
    r = client_for(is_platform).get(f"/api/enterprise/client-portal/agents/{AGENT}/history",
                                    params={"session_id": THREAD})
    assert r.status_code == 200, r.text
    body = r.json()
    # Positive control: the costed row IS in the response, so an absent key means
    # "projected out", not "no rows came back".
    assert [m["content"] for m in body["messages"]] == ["what did it cost?", "here you go"]
    assert "cost" not in set(_keys(body)), f"cost reached the wire: {json.dumps(body)[:400]}"
    assert "0.4242" not in r.text


def test_the_narrow_poll_read_carries_no_cost_either(client_for):
    """The reply poll (`?limit=`) is the read that runs every 700 ms mid-turn."""
    r = client_for(False).get(f"/api/enterprise/client-portal/agents/{AGENT}/history",
                              params={"session_id": THREAD, "limit": 2})
    assert r.status_code == 200, r.text
    assert "cost" not in set(_keys(r.json()))


def test_the_history_message_model_declares_no_cost():
    """The projection itself, so a later row-to-model path cannot reintroduce it."""
    from client_portal.models import PortalHistoryMessage
    assert "cost" not in PortalHistoryMessage.model_fields
