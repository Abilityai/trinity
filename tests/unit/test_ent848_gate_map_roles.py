"""Gate approvers by canon role — the gate map API (trinity-enterprise#848, C3).

Target: ``services/skill_gate_map_service`` — ``set_gate`` over the real
per-test database (the ent#753 ``world``) and ``GET
/api/agents/{name}/skill-gates`` through the real route and response model (the
ent#754 ``env``), with the assignments provider faked with the ent#848 seam
methods (``FakeProvider`` — the one the enforce tests seat people with).
What is pinned:

* a PUT of ``role:<id>`` is saved lower-cased when this agent's roster names
  the role; a role no row on THIS agent names is ``unknown_role``; with no
  provider (OSS) or no roster answer it is ``approver_unavailable``; a
  malformed value is ``invalid_approver`` — and nothing is written on a refusal;
* a role named on the roster but filled by nobody is saved with the
  ``approver_unassigned`` warning (T9), ``approver_reachable: false``;
* a deadline-only PUT on a gate already stored as ``role:x`` passes on OSS
  (the approver is not re-validated when it was not sent);
* GET lists roles AFTER the kinds, each with ``label`` and ``role_id`` (declared
  on the response model, so they survive it), booleans only — no person data;
  OSS lists kinds only;
* ``viewer_fills`` for a role agrees with what ``enforce`` decides for the same
  principal, on one fixture;
* a gate stored as ``role:x`` reports its reach through the SAME function
  enforce uses (``approver_people``), not ``role_addressing.resolve`` (which
  refuses role ids).
Related flow: docs/memory/feature-flows/skill-gate.md
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_harness import db_backend  # noqa: E402,F401 — `world` needs it
from test_ent753_gate_map_service import BARE, FIN, _run, world  # noqa: E402,F401 — `world` is a fixture
from test_ent754_gate_map_viewer_and_hook import (  # noqa: E402,F401 — `env` is a fixture
    AGENT, AGENT_KEY, OTHER, OWNER, _get, env,
)
from test_ent848_role_id_seam import FakeProvider  # noqa: E402

from services import assignment_provider, skill_gate_service  # noqa: E402

pytestmark = pytest.mark.unit

DANA = "dana@example.com"
ROSTER = {"roles": [{"id": "finance", "title": None}, {"id": "Head-of-Sales", "title": None}]}


@pytest.fixture
def seat(monkeypatch):
    """Register a FakeProvider for the test; `world` pins `get_provider` to
    None, so that is re-pointed at the fake as well."""
    # trinity-enterprise#881: approvers here can open the agent; reach is pinned in test_ent881.
    monkeypatch.setattr(skill_gate_service, "_can_open", lambda agent, email: True)

    def _seat(**kw):
        p = FakeProvider(**kw)
        monkeypatch.setattr(assignment_provider, "_provider", p)
        monkeypatch.setattr(assignment_provider, "get_provider", lambda: p)
        return p
    yield _seat
    assignment_provider.clear_provider()


# ---------------------------------------------------------------------------
# PUT — validation and storage
# ---------------------------------------------------------------------------


def test_a_role_on_the_roster_is_saved_lower_cased(world, seat):
    seat(roles=ROSTER, role_people={"finance": {"emails": [DANA]}})
    out = _run(world.gms.set_gate(FIN, "pay-invoice", changes={"approver": "role:Finance"}, ctx=world.ctx))
    assert out["gate"]["approver"] == "role:finance"
    assert out["gate"]["approver_reachable"] is True and out["warnings"] == []
    assert world.db.list_agent_skill_gates(FIN)[0]["approver"] == "role:finance"
    assert skill_gate_service.list_skill_gates(FIN)["pay-invoice"].approver == "role:finance"


def test_a_role_the_roster_names_in_another_case_is_known(world, seat):
    """The provider's ids are lower-cased on the way in (`Head-of-Sales`)."""
    seat(roles=ROSTER)
    out = _run(world.gms.set_gate(FIN, "deal", changes={"approver": "role:head-of-sales"}, ctx=world.ctx))
    assert out["gate"]["approver"] == "role:head-of-sales"


def test_a_named_role_nobody_fills_is_saved_with_a_warning(world, seat):
    seat(roles=ROSTER, role_people={"finance": {"emails": []}})
    out = _run(world.gms.set_gate(FIN, "pay-invoice", changes={"approver": "role:finance"}, ctx=world.ctx))
    assert out["warnings"] == ["approver_unassigned"]
    assert out["gate"]["approver_reachable"] is False
    assert [g["approver_reachable"] for g in world.gms.list_gates(FIN)["gates"]] == [False]


@pytest.mark.parametrize("roster, approver, code", [
    (ROSTER, "role:legal", "unknown_role"),             # not on this agent's roster
    ({"roles": []}, "role:finance", "unknown_role"),    # an empty roster is a real answer
    (None, "role:finance", "approver_unavailable"),     # the provider answered nothing
    (ROSTER, "role:", "invalid_approver"),
    (ROSTER, "role: finance", "invalid_approver"),
    (ROSTER, "role:finance\n", "invalid_approver"),
    (ROSTER, "role:" + "x" * 65, "invalid_approver"),
    (ROSTER, "Role:finance", "invalid_approver"),
    (ROSTER, "finance", "invalid_approver"),            # a bare id is not a role id
    (ROSTER, ["role:finance"], "invalid_approver"),
])
def test_a_bad_role_is_refused_by_name_and_writes_nothing(world, seat, roster, approver, code):
    seat(roles=roster)
    with pytest.raises(world.gms.SkillGateMapRefused) as e:
        _run(world.gms.set_gate(FIN, "pay-invoice", changes={"approver": approver}, ctx=world.ctx))
    assert (e.value.status_code, e.value.code) == (422, code)
    assert e.value.message
    assert world.db.list_agent_skill_gates(FIN) == []


def test_the_roster_read_is_for_the_agent_being_gated(world, seat):
    p = seat(roles=ROSTER)
    _run(world.gms.set_gate(FIN, "pay-invoice", changes={"approver": "role:finance"}, ctx=world.ctx))
    assert ("approver_roles_for", FIN) in p.calls


def test_oss_refuses_a_role_id(world):
    """No provider: kinds only, and a role id is `approver_unavailable`."""
    with pytest.raises(world.gms.SkillGateMapRefused) as e:
        _run(world.gms.set_gate(FIN, "pay-invoice", changes={"approver": "role:finance"}, ctx=world.ctx))
    assert (e.value.status_code, e.value.code) == (422, "approver_unavailable")


def test_a_deadline_only_put_on_a_stored_role_gate_passes_on_oss(world, seat, monkeypatch):
    seat(roles=ROSTER, role_people={"finance": {"emails": [DANA]}})
    _run(world.gms.set_gate(FIN, "pay-invoice", changes={"approver": "role:finance"}, ctx=world.ctx))
    monkeypatch.setattr(assignment_provider, "_provider", None)       # the module went away
    monkeypatch.setattr(assignment_provider, "get_provider", lambda: None)
    out = _run(world.gms.set_gate(FIN, "pay-invoice", changes={"deadline_hours": 4}, ctx=world.ctx))
    assert (out["gate"]["approver"], out["gate"]["deadline_hours"]) == ("role:finance", 4)
    assert out["warnings"] == ["approver_unassigned"]          # no provider: the role fills nobody


# ---------------------------------------------------------------------------
# GET — roles after kinds, through the response model
# ---------------------------------------------------------------------------


def _by_kind(body):
    return {a["kind"]: a for a in body["approvers"]}


def test_get_lists_roles_after_the_kinds_with_label_and_role_id(env, seat):
    seat(roles=ROSTER, people={"primary": [], "approver": []},
         role_people={"finance": {"emails": ["Viewer@Example.com"]}, "head-of-sales": {"emails": []}})
    body = _get(env, OTHER)

    assert [a["kind"] for a in body["approvers"]] == [
        "primary", "approver", "role:finance", "role:head-of-sales"]
    assert body["approver_kinds"] == ["primary", "approver"]   # stays kinds
    k = _by_kind(body)
    assert k["role:finance"] == {"kind": "role:finance", "reachable": True, "viewer_fills": True,
                                 "label": "Finance", "role_id": "finance", "unreachable": None}
    assert k["role:head-of-sales"] == {"kind": "role:head-of-sales", "reachable": False,
                                       "viewer_fills": False, "label": "Head of sales",
                                       "role_id": "head-of-sales", "unreachable": "unfilled"}
    assert k["primary"]["role_id"] is None
    assert "example.com" not in str(body["approvers"]).lower()


def test_a_provider_title_is_the_label(env, seat):
    seat(roles={"roles": [{"id": "cfo", "title": "Chief Financial Officer"}]},
         role_people={"cfo": {"emails": []}})
    assert _by_kind(_get(env, OWNER))["role:cfo"]["label"] == "Chief Financial Officer"


def test_oss_lists_kinds_only(env):
    body = _get(env, OWNER)
    assert [a["kind"] for a in body["approvers"]] == ["primary"]


def test_a_roster_read_failure_lists_kinds_only(env, seat):
    class Raising(FakeProvider):
        def approver_roles_for(self, agent_name):
            raise RuntimeError("db down")
    env.monkeypatch.setattr(assignment_provider, "_provider", Raising())
    assert [a["kind"] for a in _get(env, OWNER)["approvers"]] == ["primary", "approver"]


@pytest.mark.parametrize("user", [OWNER, OTHER, AGENT_KEY])
def test_viewer_fills_for_a_role_agrees_with_what_enforce_does(env, seat, user):
    """For the same principal, "you approve this" on a role is true exactly
    when a gated request would self-approve."""
    # OTHER (viewer@example.com) is seated in the role, in another case; the
    # agent key carries OWNER's address, and OWNER is not seated.
    seat(roles=ROSTER, role_people={"finance": {"emails": ["VIEWER@example.com"]}})
    fills = _by_kind(_get(env, user))["role:finance"]["viewer_fills"]
    gates = {"send-invoice": skill_gate_service.SkillGate(approver="role:finance", deadline_hours=None)}
    requester = skill_gate_service.requester_from_principal(user)
    try:
        decision = asyncio.run(skill_gate_service.enforce(
            AGENT, request_text="/send-invoice now", requester=requester,
            triggered_by="manual", gates=gates, refuse_only=True))
        self_approves = decision.self_approved_by is not None
    except skill_gate_service.SkillGateRefused:
        self_approves = False
    assert fills is self_approves, user.username
    assert fills is (user is OTHER)


def test_a_stored_role_gate_reports_its_reach_through_approver_people(env, seat):
    """`role_addressing.resolve` refuses a role id (`invalid_to`); the map's
    reach must come from the function enforce uses."""
    seat(roles=ROSTER, role_people={"finance": {"emails": [DANA]}})
    env.monkeypatch.setattr(
        "database.db.list_agent_skill_gates",
        lambda name: [{"skill_name": "pay-invoice", "approver": "role:finance", "deadline_hours": None,
                       "origin": "set", "set_by": "owner", "set_by_agent": None,
                       "set_at": "2026-10-13T10:00:00Z"}])
    body = _get(env, OWNER)
    assert [g["approver_reachable"] for g in body["gates"]] == [True]


# ---------------------------------------------------------------------------
# The MCP schema refuses what the backend would — one regex
# ---------------------------------------------------------------------------

_SKILLS_TS = Path(__file__).resolve().parents[2] / "src" / "mcp-server" / "src" / "tools" / "skills.ts"


@pytest.mark.parametrize("value", [
    "primary", "approver", "role:finance", "role:Finance", "role:cfo.eu_2", "role:" + "x" * 64,
    "role:", "role: finance", "role:finance\n", "role:" + "x" * 65, "role:-lead", "Role:finance",
    "finance", "PRIMARY", "viewer",
])
def test_the_mcp_approver_regex_accepts_exactly_what_the_backend_parses(value):
    """Source-text pin, on top of the executing tests on both sides
    (`skill-gates.test.ts` runs the zod schema; the PUT tests above run the
    service): its live consumer is the MCP `set_skill_gate` schema, which no
    Python test can execute. The literal is read out of `skills.ts`, compiled,
    and must agree with the backend's own parser on every case — a drift on
    either side (a widened TS class, a new kind) fails here."""
    import re
    from services import role_addressing
    from services.skill_gate_map_service import APPROVER_KINDS

    m = re.search(r"approver: z\.string\(\)\.regex\(/(.+?)/\)", _SKILLS_TS.read_text())
    assert m, "set_skill_gate.approver is no longer a regex string"
    ts = re.compile(m.group(1).replace("$", r"\Z"))          # JS `$` (no m flag) is end of input
    backend = value in APPROVER_KINDS or role_addressing.parse_role_id(value) is not None
    assert bool(ts.match(value)) is backend
    assert assignment_provider.ROLE_ID_RE.pattern in m.group(1)
