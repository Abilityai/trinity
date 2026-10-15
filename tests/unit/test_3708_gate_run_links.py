"""trinity#3708 — the gated-skill approval card links a run only for a viewer
who can open it.

A run page is `/agents/{agent}/executions/{id}`: an operator route (a
Workspace-only role is sent back to the Workspace) whose API is
`AuthorizedAgent` (no access to THAT agent → 404). The card carries two runs on
two different agents — the requester's run on the requesting agent, the
approved run on the gated agent — so `skill_gate_service.card_view` checks each
id against its own agent, and the frontend draws a link only for an id it got.

Driven over the real per-test database and the real ask sink (the #751 world):
FIN is the gated agent, REQ the requesting one, both owned by a Workspace-only
`user`.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_harness import db_backend  # noqa: E402,F401
from test_ent751_gate_dispatch import (  # noqa: E402,F401 — the shared world
    FIN, OWNER, OWNER_EMAIL, REQ, _ask_row, _raise_request, _resolve, gate,
)

import services.ask_service as _ASK  # noqa: E402
import services.skill_gate_service as _GATE  # noqa: E402
from database import db  # noqa: E402
from db_models import UserCreate  # noqa: E402

pytestmark = pytest.mark.unit

OPS, OPS_EMAIL = "gate-ops", "gate-ops@example.com"          # operator, FIN shared only
BOTH, BOTH_EMAIL = "gate-both", "gate-both@example.com"      # operator, FIN and REQ shared
ADMIN, ADMIN_EMAIL = "gate-admin", "gate-admin@example.com"  # admin: every agent


@pytest.fixture
def world(gate, monkeypatch):
    monkeypatch.setattr(_GATE, "_skill_descriptions", lambda agent: {})
    for name, role, email in ((OPS, "operator", OPS_EMAIL), (BOTH, "operator", BOTH_EMAIL),
                              (ADMIN, "admin", ADMIN_EMAIL)):
        db.create_user(UserCreate(username=name, role=role, email=email))
    db.share_agent(FIN, OWNER, OPS_EMAIL)
    db.share_agent(FIN, OWNER, BOTH_EMAIL)
    db.share_agent(REQ, OWNER, BOTH_EMAIL)
    rid = _raise_request()
    _ASK.answer(_ask_row(rid), response=_GATE.APPROVE, response_text=None,
                actor=_ASK.Actor(email=OWNER_EMAIL))
    assert _resolve(rid) == "dispatched"
    record = db.get_gate_request(rid)
    return SimpleNamespace(rid=rid, requester_run=record["requester_execution_id"],
                           approved_run=record["dispatched_execution_id"])


def _runs(world, run_viewer):
    view = _GATE.card_view(_ask_row(world.rid), viewer_email=OWNER_EMAIL, run_viewer=run_viewer)
    return view["requester"]["execution_id"], view["execution_id"]


def test_the_world_has_both_runs(world):
    assert world.requester_run and world.approved_run


@pytest.mark.parametrize("viewer,requester_link,approved_link", [
    (ADMIN, True, True),     # admin opens every agent
    (BOTH, True, True),      # operator with access to both agents
    (OPS, False, True),      # operator with no access to the REQUESTING agent → its run would 404
    (OWNER, False, False),   # owns both, but Workspace-only: the router sends them away
    ("no-such-user", False, False),
    (None, False, False),    # an external client: no platform account
])
def test_each_run_is_linked_only_for_a_viewer_who_can_open_it(world, viewer, requester_link,
                                                              approved_link):
    requester, approved = _runs(world, viewer)
    assert (requester == world.requester_run) is requester_link
    assert (approved == world.approved_run) is approved_link
    assert requester in (None, world.requester_run) and approved in (None, world.approved_run)


def test_an_unreadable_user_hides_the_links(world, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "can_user_access_agent", _boom)
    assert _runs(world, ADMIN) == (None, None)


@pytest.mark.parametrize("email,is_platform,linked", [
    (OWNER_EMAIL, True, False),   # the reported repro: a Workspace-only member's Inbox
    (BOTH_EMAIL, True, True),     # an operator in the Workspace
    (BOTH_EMAIL, False, False),   # the same address on a portal session: no platform account
])
def test_the_workspace_projection(world, email, is_platform, linked):
    from client_portal.asks import service as asks

    gate_view = asks._project(_ask_row(world.rid), viewer_email=email, is_platform=is_platform).gate
    assert (gate_view["requester"]["execution_id"] == world.requester_run) is linked
    assert (gate_view["execution_id"] == world.approved_run) is linked


def test_the_operator_queue_passes_the_viewers_own_username(world):
    from models import User
    from routers import operator_queue

    ops = User(id=7, username=OPS, email=OPS_EMAIL, role="operator")
    [row] = operator_queue._for_principal([_ask_row(world.rid)], ops)
    assert row["gate"]["requester"]["execution_id"] is None          # no access to REQ
    assert row["gate"]["execution_id"] == world.approved_run         # FIN is shared with them
