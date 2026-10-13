"""
Gated skills — the decision at the entry point (trinity-enterprise#751).

Target: ``services/skill_gate_service.enforce`` over the real per-process
SQLite and the real ask sink (``ask_service.raise_ask``). Stubbed only: the gate
map (ent#753 storage, not built yet), the in-container fingerprint exec, the
owner lookup, and the sink's audit/broadcast side effects.

What a call can end as:
  ungated            → the caller dispatches as today
  self-approved      → a PERSON who is the approver asked; the caller dispatches
  SkillApprovalRequired → an approval was raised; nothing runs
  SkillGateRefused   → a named refusal; nothing runs, nothing is raised
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

OWNER = "owner-751@example.com"


@pytest.fixture
def gate(monkeypatch):
    import json as _json
    from types import SimpleNamespace
    from database import db as real_db
    import services.ask_service as ask_svc
    import services.operator_queue_service as oqs
    import services.role_addressing as role_addressing
    import services.skill_gate_service as svc
    from services import assignment_provider
    from services.rate_limiter import RateLimitResult

    state = {"gates": {}, "fingerprints": {}, "fp_calls": [], "rate_ok": True, "map_error": None}

    class _Audit:
        async def log(self, **kw):
            return "evt"

    class _WS:
        async def broadcast(self, message):
            _json.loads(message)

    def _gates(agent):
        if state["map_error"]:
            raise state["map_error"]
        return state["gates"]

    async def _read(agent, names):
        state["fp_calls"].append((agent, tuple(names)))
        fp = state["fingerprints"]
        return None if fp is None else {n: fp.get(n, {"error": "not_found"}) for n in names}

    monkeypatch.setattr(ask_svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(ask_svc, "_websocket_manager", _WS())
    monkeypatch.setattr(role_addressing, "owner_email", lambda agent: OWNER)
    monkeypatch.setattr(oqs, "_workspace_attachment", lambda agent, email, **_: (None, False))
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda agent: False, raising=False)
    monkeypatch.setattr(oqs.rate_limiter, "check", lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    monkeypatch.setattr(svc.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(state["rate_ok"], 10, 0, 60))
    monkeypatch.setattr(svc, "list_skill_gates", _gates)
    monkeypatch.setattr(svc, "read_skill_fingerprints", _read)
    assignment_provider.clear_provider()
    yield SimpleNamespace(svc=svc, db=real_db, state=state)
    assignment_provider.clear_provider()


def _agent_requester(svc, name="marketing", execution_id="exec-mkt-1"):
    return svc.Requester(kind="agent", key=f"agent:{name}", agent_name=name,
                         execution_id=execution_id)


def _person(svc, email, *, proven=True):
    return svc.Requester(kind="person", key=f"person:{email}", email=email, is_person=proven)


def _gated(gate, agent_skills=("pay-invoice",), approver="primary", deadline_hours=None):
    gate.state["gates"] = {n: gate.svc.SkillGate(approver=approver, deadline_hours=deadline_hours)
                           for n in agent_skills}
    gate.state["fingerprints"] = {n: {"fingerprint": f"fp-{n}", "kind": "own"} for n in agent_skills}


async def _enforce(gate, agent, text, requester, **kw):
    kw.setdefault("triggered_by", "agent")
    return await gate.svc.enforce(agent, request_text=text, requester=requester, **kw)


class TestUngated:
    async def test_no_gates_on_the_agent(self, gate):
        decision = await _enforce(gate, "fin-751-u1", "/pay-invoice 100", _agent_requester(gate.svc))
        assert decision.ungated
        assert gate.state["fp_calls"] == []

    async def test_a_message_that_names_no_gated_skill(self, gate):
        _gated(gate)
        decision = await _enforce(gate, "fin-751-u2", "/weekly-report", _agent_requester(gate.svc))
        assert decision.ungated
        assert gate.db.count_pending_gate_requests("fin-751-u2") == 0

    async def test_the_approved_dispatch_itself_is_not_re_gated(self, gate):
        _gated(gate)
        decision = await _enforce(gate, "fin-751-u3", "/pay-invoice 100", _agent_requester(gate.svc),
                                  triggered_by="skill_gate")
        assert decision.ungated


class TestRaisesAnApproval:
    async def test_an_agent_request_raises_one_approval_and_freezes_the_request(self, gate):
        _gated(gate)
        agent = "fin-751-r1"
        with pytest.raises(gate.svc.SkillApprovalRequired) as info:
            await _enforce(gate, agent, "Run /pay-invoice 100 EUR to ACME", _agent_requester(gate.svc),
                           occurrence_key="occ-r1", dispatch={"model": "sonnet"})
        detail = info.value.detail()
        assert detail["status"] == "pending_approval"
        assert detail["skills"] == ["pay-invoice"]
        assert detail["approver_role"] == "primary"
        rid = detail["request_id"]
        assert rid.startswith("gate-")

        record = gate.db.get_gate_request(rid)
        assert record["state"] == "pending"
        assert record["agent_name"] == agent
        assert record["skills"] == ["pay-invoice"]
        assert record["request_text"] == "Run /pay-invoice 100 EUR to ACME"
        assert record["fingerprints"] == {"pay-invoice": "fp-pay-invoice"}
        assert record["requester_kind"] == "agent"
        assert record["source_agent"] == "marketing"
        assert record["requester_execution_id"] == "exec-mkt-1"
        assert record["dispatch"] == {"model": "sonnet"}

        row = gate.db.get_operator_queue_item_for_agent_by_request_id(agent, rid)
        assert row["id"] == record["ask_item_id"]
        assert row["raised_by"] == "gate" and row["type"] == "approval"
        assert row["to_role"] == "primary"
        assert row["options"] == ["Approve", "Reject"]
        assert row["expires_at"]
        assert row["proposal"]["skills"] == ["pay-invoice"]
        assert row["proposal"]["input"] == "Run /pay-invoice 100 EUR to ACME"

    async def test_a_person_who_is_not_the_approver_raises_an_approval(self, gate):
        _gated(gate)
        with pytest.raises(gate.svc.SkillApprovalRequired):
            await _enforce(gate, "fin-751-r2", "/pay-invoice 1", _person(gate.svc, "someone@example.com"))

    async def test_the_approvers_email_without_a_proven_person_is_not_self_approval(self, gate):
        """A channel user, a public visitor or an event loopback can carry the
        owner's email; only a principal proven to be that person self-approves."""
        _gated(gate)
        with pytest.raises(gate.svc.SkillApprovalRequired):
            await _enforce(gate, "fin-751-r3", "/pay-invoice 1", _person(gate.svc, OWNER, proven=False))

    async def test_each_occurrence_is_its_own_approval(self, gate):
        """Every schedule tick is a fresh ask — a content-keyed id would replay
        the first, already-ended ask forever."""
        _gated(gate)
        ids = set()
        for occ in ("tick-1", "tick-2"):
            with pytest.raises(gate.svc.SkillApprovalRequired) as info:
                await _enforce(gate, "fin-751-r4", "/pay-invoice 1", _agent_requester(gate.svc),
                               occurrence_key=occ)
            ids.add(info.value.request_id)
        assert len(ids) == 2

    async def test_a_retry_of_the_same_occurrence_replays_without_a_second_ask(self, gate):
        _gated(gate)
        agent = "fin-751-r5"
        ids = []
        for _ in range(2):
            with pytest.raises(gate.svc.SkillApprovalRequired) as info:
                await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc),
                               occurrence_key="same-occ")
            ids.append(info.value.request_id)
        assert ids[0] == ids[1]
        assert gate.db.count_pending_gate_requests(agent) == 1
        assert len(gate.state["fp_calls"]) == 1, "a replay must not exec again"

    async def test_the_same_key_from_another_requester_is_its_own_request(self, gate):
        """The occurrence key is caller-chosen (an Idempotency-Key). Scoped by
        requester, another caller sending the same key can neither replay nor
        observe someone else's request."""
        _gated(gate)
        agent = "fin-751-r5b"
        ids = []
        for name in ("marketing", "sales"):
            with pytest.raises(gate.svc.SkillApprovalRequired) as info:
                await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc, name),
                               occurrence_key="shared-key")
            ids.append(info.value.request_id)
        assert ids[0] != ids[1]
        assert gate.db.count_pending_gate_requests(agent) == 2

    @pytest.mark.parametrize("state", ["denied", "expired", "cancelled"])
    async def test_a_retry_after_the_decision_is_told_the_decision_not_pending(self, gate, state):
        _gated(gate)
        agent = f"fin-751-r5c-{state}"
        with pytest.raises(gate.svc.SkillApprovalRequired) as first:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc),
                           occurrence_key="occ")
        assert gate.db.transition_gate_request(first.value.request_id, state)
        with pytest.raises(gate.svc.SkillGateRefused) as again:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc),
                           occurrence_key="occ")
        assert (again.value.status_code, again.value.code) == (409, f"request_{state}")
        assert again.value.extra["request_id"] == first.value.request_id

    async def test_an_approval_that_cannot_be_raised_leaves_no_pending_record(self, gate, monkeypatch):
        """A pending record with no ask behind it would hold a cap slot forever."""
        _gated(gate)
        agent = "fin-751-r5d"

        def _boom(agent_name, ask):
            raise RuntimeError("queue down")
        monkeypatch.setattr(gate.svc, "_raise_ask", _boom)
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc),
                           occurrence_key="occ")
        assert (info.value.status_code, info.value.code) == (503, "approval_unavailable")
        assert gate.db.count_pending_gate_requests(agent) == 0

    async def test_a_refused_raise_surfaces_its_real_code(self, gate, monkeypatch):
        """#3247 F1: the sink's refusal (`AskRejected.status_code`) reaches the
        caller as that code — not an AttributeError turned into a 503."""
        import services.ask_service as ask_svc
        _gated(gate)
        agent = "fin-751-r5e"

        def _refuse(agent_name, ask):
            raise ask_svc.AskRejected(409, "already_pending", "dup", request_id="x-1")
        monkeypatch.setattr(gate.svc, "_raise_ask", _refuse)
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc),
                           occurrence_key="occ")
        assert (info.value.status_code, info.value.code) == (409, "already_pending")
        assert info.value.extra == {"request_id": "x-1"}
        assert gate.db.count_pending_gate_requests(agent) == 0

    async def test_the_request_text_is_sanitised_before_it_is_stored_or_shown(self, gate):
        _gated(gate)
        secret = "ghp_" + "A" * 36
        agent = "fin-751-r7"
        with pytest.raises(gate.svc.SkillApprovalRequired) as info:
            await _enforce(gate, agent, f"/pay-invoice token={secret}", _agent_requester(gate.svc))
        record = gate.db.get_gate_request(info.value.request_id)
        assert secret not in record["request_text"]
        row = gate.db.get_operator_queue_item_for_agent_by_request_id(agent, info.value.request_id)
        assert secret not in row["proposal"]["input"]
        assert secret not in (row["question"] or "")

    async def test_the_card_carries_the_whole_request_and_the_question_a_preview(self, gate):
        # Approve runs `proposal.input`, so the approver must see all of it.
        _gated(gate)
        agent = "fin-751-r8"
        text = "/pay-invoice " + "x" * 3000
        with pytest.raises(gate.svc.SkillApprovalRequired) as info:
            await _enforce(gate, agent, text, _agent_requester(gate.svc))
        assert gate.db.get_gate_request(info.value.request_id)["request_text"] == text
        row = gate.db.get_operator_queue_item_for_agent_by_request_id(agent, info.value.request_id)
        assert row["proposal"]["input"] == text
        assert len(row["question"]) < len(text)
        assert "full request is shown below" in row["question"]

    @pytest.mark.parametrize("n, refused", [(900, False), (1100, True)])
    async def test_the_limit_is_the_cards_bytes_not_the_characters(self, gate, n, refused):
        # 1100 euro signs are far under 6000 characters but over 6000 JSON bytes:
        # a character-count limit would raise a card the queue then cannot hold.
        _gated(gate)
        agent = f"fin-751-r9-{n}"
        text = "/pay-invoice " + "\u20ac" * n
        assert len(text) < gate.svc.CARD_INPUT_MAX_BYTES
        if refused:
            with pytest.raises(gate.svc.SkillGateRefused) as info:
                await _enforce(gate, agent, text, _agent_requester(gate.svc))
            assert (info.value.status_code, info.value.code) == (422, "request_too_long")
            assert gate.db.count_pending_gate_requests(agent) == 0
            assert gate.state["fp_calls"] == []
        else:
            with pytest.raises(gate.svc.SkillApprovalRequired) as info:
                await _enforce(gate, agent, text, _agent_requester(gate.svc))
            row = gate.db.get_operator_queue_item_for_agent_by_request_id(agent, info.value.request_id)
            assert row["proposal"]["input"] == text


class TestSelfApproval:
    async def test_the_approver_asking_as_a_proven_person_runs_directly(self, gate):
        _gated(gate)
        agent = "fin-751-s1"
        decision = await _enforce(gate, agent, "/pay-invoice 1", _person(gate.svc, OWNER))
        assert not decision.ungated
        assert decision.self_approved_by == OWNER
        assert decision.skills == ("pay-invoice",)
        assert gate.db.count_pending_gate_requests(agent) == 0
        assert gate.state["fp_calls"] == []

    async def test_email_comparison_ignores_case(self, gate):
        _gated(gate)
        decision = await _enforce(gate, "fin-751-s2", "/pay-invoice 1", _person(gate.svc, OWNER.upper()))
        assert decision.self_approved_by


class TestRefusals:
    async def test_an_unreadable_gate_map_refuses(self, gate):
        gate.state["map_error"] = RuntimeError("db down")
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, "fin-751-x1", "/pay-invoice 1", _agent_requester(gate.svc))
        assert (info.value.status_code, info.value.code) == (503, "gate_unavailable")

    async def test_an_unreadable_skill_refuses_and_raises_nothing(self, gate):
        _gated(gate)
        gate.state["fingerprints"] = None
        agent = "fin-751-x2"
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc))
        assert (info.value.status_code, info.value.code) == (409, "agent_unavailable")
        assert gate.db.count_pending_gate_requests(agent) == 0

    async def test_a_gated_skill_the_agent_does_not_have_refuses(self, gate):
        _gated(gate)
        gate.state["fingerprints"] = {}
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, "fin-751-x3", "/pay-invoice 1", _agent_requester(gate.svc))
        assert info.value.code == "gated_skill_not_installed"

    async def test_an_ambiguous_skill_refuses(self, gate):
        _gated(gate)
        gate.state["fingerprints"] = {"pay-invoice": {"error": "ambiguous"}}
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, "fin-751-x4", "/pay-invoice 1", _agent_requester(gate.svc))
        assert info.value.code == "gated_skill_ambiguous"

    async def test_nobody_in_the_role_refuses_before_anything_is_written(self, gate):
        _gated(gate, approver="approver")          # no assignments provider in OSS
        agent = "fin-751-x5"
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc))
        assert (info.value.status_code, info.value.code) == (422, "role_unassigned")
        assert gate.db.count_pending_gate_requests(agent) == 0
        assert gate.state["fp_calls"] == []

    async def test_skills_with_different_approvers_are_refused_together(self, gate):
        gate.state["gates"] = {"pay-invoice": gate.svc.SkillGate(approver="primary"),
                               "publish": gate.svc.SkillGate(approver="operator")}
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, "fin-751-x6", "/pay-invoice then /publish", _agent_requester(gate.svc))
        assert info.value.code == "mixed_approvers"

    async def test_refuse_only_entries_never_raise_an_approval(self, gate):
        _gated(gate)
        agent = "fin-751-x7"
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc), refuse_only=True)
        assert (info.value.status_code, info.value.code) == (403, "approval_not_available_here")
        assert gate.db.count_pending_gate_requests(agent) == 0


class TestCaps:
    async def test_ten_pending_from_one_requester_is_the_limit(self, gate):
        _gated(gate)
        agent = "fin-751-c1"
        for i in range(gate.svc.MAX_PENDING_PER_REQUESTER):
            with pytest.raises(gate.svc.SkillApprovalRequired):
                await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc))
        fp_before = len(gate.state["fp_calls"])
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc))
        assert (info.value.status_code, info.value.code) == (429, "approval_queue_full")
        assert len(gate.state["fp_calls"]) == fp_before, "the cap must refuse before the exec"
        with pytest.raises(gate.svc.SkillApprovalRequired):
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc, name="sales"))

    async def test_the_executor_wide_limit(self, gate, monkeypatch):
        _gated(gate)
        monkeypatch.setattr(gate.svc, "MAX_PENDING_PER_EXECUTOR", 2)
        agent = "fin-751-c2"
        for name in ("a", "b"):
            with pytest.raises(gate.svc.SkillApprovalRequired):
                await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc, name=name))
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, agent, "/pay-invoice 1", _agent_requester(gate.svc, name="c"))
        assert info.value.code == "approval_queue_full"

    async def test_the_per_requester_rate(self, gate):
        _gated(gate)
        gate.state["rate_ok"] = False
        with pytest.raises(gate.svc.SkillGateRefused) as info:
            await _enforce(gate, "fin-751-c3", "/pay-invoice 1", _agent_requester(gate.svc))
        assert (info.value.status_code, info.value.code) == (429, "approval_rate_limited")
