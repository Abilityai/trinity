"""Gate approvers by canon role — enforcement and the hook
(trinity-enterprise#848, C2).

Target: ``services/skill_gate_service.enforce`` with a gate whose approver is
``role:<id>``, over the real per-process SQLite and the real ask sink (the
ent#751 world), the assignments provider faked with the ent#848 seam methods.
What is pinned:

* a PERSON the provider seats in the role (a `primary` / `approver` row — the
  provider decides that; the gate only reads its answer) self-approves, exactly
  as `approver_people` — the Skills tab's `viewer_fills` — says;
* anyone else's request raises ONE ask addressed to the role (`to_role ==
  "role:finance"`, the resolved people pre-listed), whose 202 body carries the
  role and its label — the notice sentence still names no role (trinity#3274);
* a role nobody fills, no provider, no answer or a raising provider refuses
  `role_unassigned` before anything is written (no owner fallback);
* `ROLES` / the ask sink's `to` are untouched: an AGENT addressing an ask to a
  role id is still `invalid_to`, and only a gate raise may pre-list addressees;
* the ask carries the very list `enforce` decided with (T10), for kinds too —
  the provider is read once per request, never twice;
* the in-container hook (`check_invocation`) needs no resolver of its own: it
  honours the `self_approved` record `enforce` + `record_self_approval` wrote
  for a role-gated skill, and still refuses any other run of the skill.
Related flow: docs/memory/feature-flows/skill-gate.md
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ent751_skill_gate_enforce import (  # noqa: E402,F401 — `gate` is a fixture
    OWNER, _agent_requester, _enforce, _gated, _person, gate,
)
from test_ent848_role_id_seam import FakeProvider  # noqa: E402

from services import assignment_provider  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

DANA = "dana@example.com"
XAVI = "xavi@example.com"
FINANCE = "role:finance"


def _seat(**kw) -> FakeProvider:
    p = FakeProvider(**kw)
    assignment_provider.register_provider(p)
    return p


def _role_gate(gate, approver=FINANCE):
    _gated(gate, approver=approver)


def _ask_row(gate, agent, info):
    return gate.db.get_operator_queue_item_for_agent_by_request_id(agent, info.value.request_id)


# ---------------------------------------------------------------------------
# Self-approval — the person the role seats
# ---------------------------------------------------------------------------


class TestSelfApproval:
    async def test_a_person_seated_in_the_role_self_approves(self, gate):
        _seat(role_people={"finance": {"emails": ["Dana@Example.com"]}})
        _role_gate(gate)
        decision = await _enforce(gate, "fin-848-s1", "/pay-invoice 100", _person(gate.svc, "DANA@example.com"))
        assert decision.skills == ("pay-invoice",)
        assert decision.self_approved_by == "DANA@example.com"
        assert gate.db.count_pending_gate_requests("fin-848-s1") == 0

    async def test_the_stored_id_is_matched_lower_cased(self, gate):
        """`role:Finance` on the gate reaches the provider as `finance`."""
        p = _seat(role_people={"finance": {"emails": [DANA]}})
        _role_gate(gate, approver="role:Finance")
        decision = await _enforce(gate, "fin-848-s2", "/pay-invoice 1", _person(gate.svc, DANA))
        assert decision.self_approved_by == DANA
        assert p.calls == [("people_for_role_id", "fin-848-s2", "finance")]

    @pytest.mark.parametrize("email, fills", [
        ("Dana@Example.com", True),          # mixed case, seated
        (XAVI, False),                        # a person seated under another role
        (OWNER, False),                       # the owner is not the role
    ])
    async def test_self_approval_agrees_with_viewer_fills(self, gate, email, fills):
        """The Skills tab's "you approve this" (`approver_people`, ent#754)
        and the run's self-approval are one function."""
        _seat(role_people={"finance": {"emails": [DANA]}, "ops": {"emails": [XAVI]}})
        _role_gate(gate)
        agent = "fin-848-s3"
        people = gate.svc.approver_people(agent, FINANCE)
        assert (email.casefold() in people) is fills
        try:
            decision = await _enforce(gate, agent, "/pay-invoice 1", _person(gate.svc, email), refuse_only=True)
            outcome = decision.self_approved_by is not None
        except gate.svc.SkillGateRefused as e:
            assert e.code == "approval_not_available_here"
            outcome = False
        assert outcome is fills

    async def test_an_agent_key_carrying_a_seated_email_never_self_approves(self, gate):
        _seat(role_people={"finance": {"emails": [DANA]}})
        _role_gate(gate)
        requester = gate.svc.Requester(kind="agent", key="agent:sib", agent_name="sib", email=DANA,
                                       is_person=False, execution_id="exec-sib")
        with pytest.raises(gate.svc.SkillApprovalRequired):
            await _enforce(gate, "fin-848-s4", "/pay-invoice 1", requester)


# ---------------------------------------------------------------------------
# The ask — addressed to the role, delivered to its people
# ---------------------------------------------------------------------------


class TestTheAsk:
    async def test_an_agent_request_raises_one_ask_addressed_to_the_role(self, gate):
        p = _seat(role_people={"finance": {"emails": ["Dana@Example.com", XAVI]}})
        _role_gate(gate)
        agent = "fin-848-a1"
        with pytest.raises(gate.svc.SkillApprovalRequired) as info:
            await _enforce(gate, agent, "Run /pay-invoice 100 EUR", _agent_requester(gate.svc),
                           occurrence_key="occ-a1")
        detail = info.value.detail()
        assert detail["approver_role"] == FINANCE
        assert detail["approver_label"] == "Finance"
        assert "finance" not in detail["message"].lower()          # trinity#3274: no role in the sentence
        assert "Finance" not in detail["message"]

        row = _ask_row(gate, agent, info)
        assert row["raised_by"] == "gate" and row["type"] == "approval"
        assert row["to_role"] == FINANCE
        assert sorted(row["resolved_to"]) == [DANA, XAVI]
        assert row["addressed_to_email"] is None                   # several people: no single addressee
        assert p.calls == [("people_for_role_id", agent, "finance")]   # read ONCE (T10)

    async def test_one_person_is_the_single_addressee(self, gate):
        _seat(role_people={"finance": {"emails": [DANA]}})
        _role_gate(gate)
        agent = "fin-848-a2"
        with pytest.raises(gate.svc.SkillApprovalRequired) as info:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc), occurrence_key="occ-a2")
        row = _ask_row(gate, agent, info)
        assert (row["to_role"], row["resolved_to"], row["addressed_to_email"]) == (FINANCE, [DANA], DANA)

    async def test_a_person_outside_the_role_waits_like_anyone_else(self, gate):
        _seat(role_people={"finance": {"emails": [DANA]}})
        _role_gate(gate)
        agent = "fin-848-a3"
        with pytest.raises(gate.svc.SkillApprovalRequired) as info:
            await _enforce(gate, agent, "/pay-invoice 1", _person(gate.svc, XAVI))
        assert _ask_row(gate, agent, info)["resolved_to"] == [DANA]

    async def test_a_retry_of_the_occurrence_replays_with_the_label(self, gate):
        _seat(role_people={"finance": {"emails": [DANA]}})
        _role_gate(gate)
        agent = "fin-848-a4"
        with pytest.raises(gate.svc.SkillApprovalRequired) as first:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc), occurrence_key="occ-a4")
        with pytest.raises(gate.svc.SkillApprovalRequired) as again:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc), occurrence_key="occ-a4")
        assert again.value.request_id == first.value.request_id
        assert again.value.detail()["approver_label"] == "Finance"
        assert gate.db.count_pending_gate_requests(agent) == 1

    async def test_a_kind_gate_carries_the_list_enforce_decided_with(self, gate):
        """T10 applies to kinds too: the ask's audience is the pre-resolved
        list, so the owner fallback for `primary` is read once, not twice."""
        _gated(gate, approver="primary")
        agent = "fin-848-a5"
        with pytest.raises(gate.svc.SkillApprovalRequired) as info:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc), occurrence_key="occ-a5")
        row = _ask_row(gate, agent, info)
        assert (row["to_role"], row["resolved_to"], row["addressed_to_email"]) == ("primary", [OWNER], OWNER)
        assert info.value.detail()["approver_label"] == "primary"


# ---------------------------------------------------------------------------
# Refusals — fail closed, nothing written
# ---------------------------------------------------------------------------


class TestRefusals:
    @pytest.mark.parametrize("setup", ["no_provider", "predates", "nobody", "no_answer", "raises"])
    async def test_a_role_nobody_fills_refuses_before_anything_is_written(self, gate, setup):
        if setup == "predates":
            class Old:
                def assignment_for(self, agent_name, triggered_by):
                    return None
            assignment_provider.register_provider(Old())
        elif setup == "nobody":
            _seat(role_people={"finance": {"emails": []}})
        elif setup == "no_answer":
            _seat(role_people={})
        elif setup == "raises":
            class Boom(FakeProvider):
                def people_for_role_id(self, agent_name, role_id):
                    raise RuntimeError("db down")
            assignment_provider.register_provider(Boom())
        _role_gate(gate)
        agent = f"fin-848-r-{setup}"
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc))
        assert (info.value.status_code, info.value.code) == (422, "role_unassigned")
        assert "Finance" in str(info.value) and info.value.extra["role"] == FINANCE
        assert gate.db.count_pending_gate_requests(agent) == 0
        assert gate.state["fp_calls"] == []

    async def test_a_seated_person_cannot_self_approve_an_unfilled_role_either(self, gate):
        _seat(role_people={"finance": {"emails": []}})
        _role_gate(gate)
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, "fin-848-r2", "/pay-invoice 1", _person(gate.svc, DANA))
        assert info.value.code == "role_unassigned"

    async def test_a_role_gate_and_a_kind_gate_on_one_request_refuse_together(self, gate):
        _seat(role_people={"finance": {"emails": [OWNER]}})      # even a person filling both
        gate.state["gates"] = {"pay-invoice": gate.svc.SkillGate(approver=FINANCE),
                               "wire-funds": gate.svc.SkillGate(approver="primary")}
        gate.state["fingerprints"] = {n: {"fingerprint": f"fp-{n}", "kind": "own"} for n in gate.state["gates"]}
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, "fin-848-r3", "/pay-invoice then /wire-funds", _person(gate.svc, OWNER))
        assert info.value.code == "mixed_approvers"

    async def test_two_role_gates_with_one_role_are_one_request(self, gate):
        _seat(role_people={"finance": {"emails": [DANA]}})
        gate.state["gates"] = {"pay-invoice": gate.svc.SkillGate(approver=FINANCE),
                               "wire-funds": gate.svc.SkillGate(approver="role:Finance")}
        gate.state["fingerprints"] = {n: {"fingerprint": f"fp-{n}", "kind": "own"} for n in gate.state["gates"]}
        # identity is the STORED string: a differently-cased value is a different approver today
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, "fin-848-r4", "/pay-invoice then /wire-funds", _person(gate.svc, DANA))
        assert info.value.code == "mixed_approvers"


# ---------------------------------------------------------------------------
# The ask sink — agents stay on kinds; only a gate pre-lists addressees
# ---------------------------------------------------------------------------


class TestAskSink:
    def _body(self, rid, **over):
        return {"request_id": rid, "type": "question", "title": "Which budget?",
                "question": "Which budget line?", "to": "primary", **over}

    async def test_an_agent_addressing_a_role_id_is_still_invalid_to(self, gate):
        import services.ask_service as ask
        _seat(role_people={"finance": {"emails": [DANA]}})
        with pytest.raises(ask.AskRejected) as info:
            ask.raise_ask("fin-848-k1", self._body("ask-k1", to=FINANCE), raised_by="agent", channel="mcp")
        assert (info.value.status_code, info.value.code) == (422, "invalid_to")

    @pytest.mark.parametrize("kw", [
        {"addressees": [DANA]},
        {"addressees": [DANA], "to_role_override": FINANCE},
    ])
    async def test_an_agent_raise_may_not_pre_list_its_audience(self, gate, kw):
        import services.ask_service as ask
        with pytest.raises(ValueError):
            ask.raise_ask("fin-848-k2", self._body("ask-k2"), raised_by="agent", channel="mcp", **kw)

    @pytest.mark.parametrize("kw", [
        {"addressees": []},                                   # a gate never asks nobody
        {"addressees": ["not-an-email"]},
        {"to_role_override": FINANCE},                        # an override without the list it stands for
        {"addressees": [DANA], "addressee": DANA},            # one way or the other, never both
    ])
    async def test_a_malformed_gate_audience_is_a_programming_error(self, gate, kw):
        import services.ask_service as ask
        with pytest.raises(ValueError):
            ask.raise_ask("fin-848-k3", self._body("gate-k3", type="approval", options=["Approve", "Reject"]),
                          raised_by="gate", channel="gate", **kw)

    async def test_a_gate_raise_with_addressees_skips_role_resolution(self, gate):
        import services.ask_service as ask
        import services.role_addressing as ra
        calls = []
        gate_monkeypatch = pytest.MonkeyPatch()
        gate_monkeypatch.setattr(ra, "resolve", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("resolved")))
        try:
            receipt = ask.raise_ask(
                "fin-848-k4", self._body("gate-k4", type="approval", options=["Approve", "Reject"], to="approver"),
                raised_by="gate", channel="gate", addressees=[" Dana@Example.com ", DANA, XAVI],
                to_role_override=FINANCE)
        finally:
            gate_monkeypatch.undo()
        assert calls == []
        row = gate.db.get_operator_queue_item_for_agent_by_request_id("fin-848-k4", "gate-k4")
        assert row["to_role"] == FINANCE and row["resolved_to"] == [DANA, XAVI]
        assert receipt["to_role"] == FINANCE                 # the receipt names the role it went to


# ---------------------------------------------------------------------------
# The hook — honours the record enforce wrote, refuses every other run
# ---------------------------------------------------------------------------


@pytest.fixture
def hook(gate, monkeypatch):
    runs = {}                                                   # execution id → (agent, status)
    monkeypatch.setattr(gate.db, "get_execution_gate_state", lambda eid: runs.get(eid))

    async def _no_audit(*a, **k):
        return None
    monkeypatch.setattr(gate.svc, "_audit_refusal", _no_audit)
    monkeypatch.setattr(gate.svc, "audit_self_approved", _no_audit)
    gate.runs = runs
    return gate


async def _check(gate, agent, execution_id):
    return await gate.svc.check_invocation(agent, via="skill", invoked="pay-invoice", names=["pay-invoice"],
                                           resolved=True, execution_id=execution_id)


class TestHook:
    async def test_the_self_approved_run_is_cleared_and_no_other_run_is(self, hook):
        """The hook resolves no approver: it reads the record `enforce` +
        `record_self_approval` wrote. Proven on one world: the cleared run is
        allowed, a second live run of the same agent is refused `not_cleared`,
        and another agent's run — even one that is live — is refused."""
        _seat(role_people={"finance": {"emails": [DANA]}})
        _role_gate(hook)
        agent = "fin-848-h1"
        decision = await _enforce(hook, agent, "/pay-invoice 1", _person(hook.svc, DANA))
        await hook.svc.record_self_approval(agent, decision, execution_id="exec-848-h1",
                                            current_user=None, endpoint="/task", request_text="/pay-invoice 1")
        record = hook.db.get_gate_request_by_dispatched_execution("exec-848-h1")
        assert record["state"] == "self_approved" and record["skills"] == ["pay-invoice"]

        hook.runs.update({"exec-848-h1": (agent, "running"), "exec-848-h2": (agent, "running"),
                          "exec-848-h3": ("other-agent", "running")})
        assert await _check(hook, agent, "exec-848-h1") == {"allowed": True, "gated": True, "message": None}

        other = await _check(hook, agent, "exec-848-h2")
        assert (other["allowed"], other["gated"]) == (False, True) and "pay-invoice" in other["message"]

        foreign = await _check(hook, agent, "exec-848-h3")
        assert foreign["allowed"] is False

    async def test_the_hook_decides_without_the_provider(self, hook):
        """Once the record exists the provider may be gone (the entitlement
        lapsed, the module unloaded): the cleared run still runs — the record,
        not a fresh resolution, is what the hook honours."""
        _seat(role_people={"finance": {"emails": [DANA]}})
        _role_gate(hook)
        agent = "fin-848-h2"
        decision = await _enforce(hook, agent, "/pay-invoice 1", _person(hook.svc, DANA))
        await hook.svc.record_self_approval(agent, decision, execution_id="exec-848-h2a",
                                            current_user=None, endpoint="/task")
        assignment_provider.clear_provider()
        hook.runs["exec-848-h2a"] = (agent, "running")
        assert (await _check(hook, agent, "exec-848-h2a"))["allowed"] is True

    async def test_a_waiting_request_clears_nothing(self, hook):
        _seat(role_people={"finance": {"emails": [DANA]}})
        _role_gate(hook)
        agent = "fin-848-h3"
        with pytest.raises(hook.svc.SkillApprovalRequired):
            await _enforce(hook, agent, "/pay-invoice 1", _agent_requester(hook.svc), occurrence_key="occ-h3")
        hook.runs["exec-848-h3"] = (agent, "running")
        assert (await _check(hook, agent, "exec-848-h3"))["allowed"] is False


# ---------------------------------------------------------------------------
# card_view — the Inbox card names the role, never a person (T11)
# ---------------------------------------------------------------------------


class TestCardView:
    async def test_the_card_carries_the_role_and_its_label(self, gate, monkeypatch):
        monkeypatch.setattr(gate.svc, "_skill_descriptions", lambda agent: {})
        _seat(role_people={"finance": {"emails": [DANA]}})
        _role_gate(gate)
        agent = "fin-848-c1"
        with pytest.raises(gate.svc.SkillApprovalRequired) as info:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc), occurrence_key="occ-c1")
        row = _ask_row(gate, agent, info)
        view = gate.svc.card_view(row, viewer_email=XAVI, name_approvers=False)
        assert view["waiting_role"] == {"value": FINANCE, "label": "Finance"}
        assert view["waiting_on"] == [] and view["can_decide"] is False
        assert DANA not in str(view)

    async def test_a_kind_gate_card_names_the_kind(self, gate, monkeypatch):
        monkeypatch.setattr(gate.svc, "_skill_descriptions", lambda agent: {})
        _gated(gate, approver="primary")
        agent = "fin-848-c2"
        with pytest.raises(gate.svc.SkillApprovalRequired) as info:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc), occurrence_key="occ-c2")
        view = gate.svc.card_view(_ask_row(gate, agent, info), viewer_email=OWNER)
        assert view["waiting_role"] == {"value": "primary", "label": "primary"}
