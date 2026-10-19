"""trinity-enterprise#881 — a gated skill's approval goes only to approvers who
can open the agent, and refuses by name when none can.

A person decides a gated approval in the Workspace (the agent must be on their
roster: owned or shared) or in Operations (an operator or admin with access to
the agent). An approver who can do neither never sees the card, so raising the
approval to them was a silent wait until it expired.

Driven over the real per-test database and the real ask sink (the #751 world):
FIN is the gated agent, owned by a Workspace-only `user`. The gate's approver is
the `approver` role, answered by a fake assignments provider.
"""
import asyncio
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
    FIN, OWNER, REQ, _ask_row, _raise_request, gate,
)

import services.skill_gate_map_service as _MAP  # noqa: E402
import services.skill_gate_service as _GATE  # noqa: E402
from database import db  # noqa: E402
from db_models import UserCreate  # noqa: E402

pytestmark = pytest.mark.unit

UNSHARED = "unshared@example.com"   # platform user, FIN not shared with them
SHARED = "shared@example.com"       # platform user, FIN shared with them
CLIENT = "client@example.com"       # no platform account, FIN shared with them
STRANGER = "stranger@example.com"   # no platform account, no share
ADMIN = "admin881@example.com"      # admin: opens every agent


class _Provider:
    def __init__(self, approvers):
        self.approvers = approvers

    def assignment_for(self, agent_name, triggered_by):
        return None

    def people_for(self, agent_name, role):
        return {"emails": list(self.approvers)} if role == "approver" else None


@pytest.fixture
def world(gate, monkeypatch):
    from services import assignment_provider

    for username, role, email in (("p-unshared", "user", UNSHARED), ("p-shared", "user", SHARED),
                                  ("p-admin", "admin", ADMIN)):
        db.create_user(UserCreate(username=username, role=role, email=email))
    db.share_agent(FIN, OWNER, SHARED)
    db.share_agent(FIN, OWNER, CLIENT)
    monkeypatch.setattr(_GATE, "list_skill_gates",
                        lambda agent: {"pay-invoice": _GATE.SkillGate(approver="approver")}
                        if agent == FIN else {})

    def approvers(*emails):
        assignment_provider.clear_provider()
        assignment_provider.register_provider(_Provider(emails))

    yield SimpleNamespace(approvers=approvers)
    assignment_provider.clear_provider()


def _refusal():
    async def _go():
        await _GATE.enforce(FIN, request_text="/pay-invoice 100 EUR",
                            requester=_GATE.Requester(kind="agent", key=f"agent:{REQ}",
                                                      agent_name=REQ, execution_id="exec-origin"),
                            triggered_by="agent", dispatch={"triggered_by": "agent"})
    with pytest.raises(_GATE.SkillGateRefused) as exc:
        asyncio.run(_go())
    return exc.value


@pytest.mark.parametrize("email,reachable", [
    (SHARED, True), (CLIENT, True), (ADMIN, True),
    (UNSHARED, False), (STRANGER, False),
])
def test_who_can_open_the_agent(world, email, reachable):
    world.approvers(email)
    assert (_GATE.approver_people(FIN, "approver") == [email]) is reachable


def test_an_approval_nobody_can_open_is_refused_by_name_and_raises_nothing(world):
    world.approvers(UNSHARED, STRANGER)
    e = _refusal()
    assert (e.status_code, e.code) == (422, "approver_cannot_open_agent")
    assert UNSHARED not in str(e) and STRANGER not in str(e)   # the role, never a person
    assert "share" in str(e).lower()
    assert db.list_pending_gate_requests(FIN) == []


def test_an_approval_goes_only_to_the_approvers_who_can_open_the_agent(world):
    world.approvers(UNSHARED, SHARED)
    row = _ask_row(_raise_request())
    resolved = row["resolved_to"]
    assert [p.casefold() for p in (resolved if isinstance(resolved, list) else [])] == [SHARED]


def test_an_unfilled_role_keeps_its_own_refusal(world):
    world.approvers()
    assert _refusal().code == "role_unassigned"


def test_the_shared_approver_finds_it_in_their_workspace(world):
    from client_portal.asks import service as asks

    world.approvers(UNSHARED, SHARED)
    row = _ask_row(_raise_request())
    mine = asks.list_asks_page(SHARED, True, FIN).items
    assert [a.id for a in mine] == [row["id"]]


@pytest.mark.parametrize("approvers,reachable,reason", [
    ((SHARED,), True, None),
    ((UNSHARED,), False, "not_shared"),
    ((), False, "unfilled"),
])
def test_the_skills_tab_says_why_an_approver_is_unreachable(world, approvers, reachable, reason):
    world.approvers(*approvers)
    entry = next(a for a in _MAP.approver_status(FIN, None) if a["kind"] == "approver")
    assert (entry["reachable"], entry["unreachable"]) == (reachable, reason)

    db.write_skill_gate(FIN, "pay-invoice", changes={"approver": "approver"}, origin="explicit",
                        set_by=OWNER, set_by_agent=None)
    [g] = _MAP.list_gates(FIN)["gates"]
    assert (g["approver_reachable"], g["approver_unreachable"]) == (reachable, reason)
