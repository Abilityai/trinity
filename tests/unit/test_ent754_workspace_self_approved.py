"""trinity-enterprise#754 (C9) — a Workspace turn that skipped approval says so.

The 10-03 eyeball that asked for the marker happened in the Workspace: the
person who fills a gated skill's approver role asked for it in chat, the gate
self-approved, and the reply landed with no sign a gate applied. Since #3166
every Workspace message records the turn (`execution_id`) that wrote it, so the
history read can say, per agent reply, what ent#752's `self_approved` record
says about that run — the same derivation the Tasks list uses, no new column.

Pinned here through the REAL `client_portal.service.get_history` over a real
database, with the self-approval written by the REAL `record_self_approval`:

* only the reply of the self-approved turn is marked — not its question, not
  another turn's reply, not a row no turn wrote (a report);
* "by viewer" is decided from the portal session's email, casefolded;
* the reply poll's narrow read (`limit`) carries the marker too, so a reply
  that has just landed shows it without a reload;
* the route's response model declares both fields (an undeclared key is
  stripped by `response_model`), and no email is added to the payload.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("fastapi", reason="backend venv required")

from db_harness import db_backend, seed_execution  # noqa: E402,F401

pytestmark = pytest.mark.unit

AGENT = "finance"
THREAD = "thread-754"
ANA = "ana@example.com"


def _row(role, text, execution_id, at):
    from client_portal import db as pdb
    pdb.add_portal_message(uuid.uuid4().hex, AGENT, ANA, role, text, None, at,
                           session_id=THREAD, execution_id=execution_id)


@pytest.fixture
def thread(db_backend, monkeypatch):
    from client_portal import db as pdb
    from client_portal import service as svc
    from services import skill_gate_service

    async def _no_audit(*a, **kw):
        return None

    monkeypatch.setattr(skill_gate_service, "audit_self_approved", _no_audit)
    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_attach_own_ratings", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "get_turn_inflight", lambda sid: None)

    pdb.create_portal_session(THREAD, AGENT, ANA, "2026-10-08T09:00:00.000000Z")
    seed_execution("__manual__", AGENT, exec_id="exec-self", triggered_by="portal")
    seed_execution("__manual__", AGENT, exec_id="exec-plain", triggered_by="portal")
    _row("user", "/pay-invoice 100 EUR", "exec-self", "2026-10-08T09:01:00.000000Z")
    _row("assistant", "Paid.", "exec-self", "2026-10-08T09:02:00.000000Z")
    _row("user", "and the weather?", "exec-plain", "2026-10-08T09:03:00.000000Z")
    _row("assistant", "Sunny.", "exec-plain", "2026-10-08T09:04:00.000000Z")
    _row("assistant", "Weekly report is ready.", None, "2026-10-08T09:05:00.000000Z")

    # The Workspace's gate requester is the portal person (mixed case here).
    decision = skill_gate_service.GateDecision(("pay-invoice",), self_approved_by="Ana@Example.com")
    asyncio.run(skill_gate_service.record_self_approval(
        AGENT, decision, execution_id="exec-self", current_user=None,
        endpoint="execute_task:portal", request_text="/pay-invoice 100 EUR", triggered_by="portal"))
    return svc


def _flags(messages):
    return [(m["role"], m["content"], m.get("gate_self_approved"), m.get("gate_self_approved_by_viewer"))
            for m in messages]


def test_only_the_self_approved_reply_is_marked(thread):
    out = thread.get_history(AGENT, ANA, session_id=THREAD)

    assert _flags(out["messages"]) == [
        ("user", "/pay-invoice 100 EUR", False, False),
        ("assistant", "Paid.", True, True),
        ("user", "and the weather?", False, False),
        ("assistant", "Sunny.", False, False),
        ("assistant", "Weekly report is ready.", False, False),
    ]


def test_the_reply_poll_carries_it_too(thread):
    """`limit` is the reply poll's narrow read — the path a just-finished
    turn's reply arrives on, before any reload."""
    out = thread.get_history(AGENT, ANA, session_id=THREAD, limit=4)

    paid = next(m for m in out["messages"] if m["content"] == "Paid.")
    assert (paid["gate_self_approved"], paid["gate_self_approved_by_viewer"]) == (True, True)


def test_the_route_model_keeps_both_fields_and_adds_no_email(thread):
    from client_portal.models import PortalHistory

    out = thread.get_history(AGENT, ANA, session_id=THREAD)
    shaped = PortalHistory.model_validate(out).model_dump()

    paid = next(m for m in shaped["messages"] if m["content"] == "Paid.")
    assert paid["gate_self_approved"] is True
    assert paid["gate_self_approved_by_viewer"] is True
    new_keys = {k for k in paid if "self_approved" in k}
    assert new_keys == {"gate_self_approved", "gate_self_approved_by_viewer"}
    assert "ana@example.com" not in str({k: paid[k] for k in new_keys}).lower()


def test_through_the_route_a_machine_viewer_sees_neither(thread):
    """A system-scoped key on the platform session is admitted (`is_person`
    False); as on the executions path, the marker is for people only."""
    from client_portal import router as portal_router
    from client_portal.portal_auth import PortalPrincipal

    def paid(principal):
        out = portal_router.portal_history(AGENT, session_id=THREAD, limit=None, principal=principal)
        m = next(m for m in out["messages"] if m["content"] == "Paid.")
        return m["gate_self_approved"], m["gate_self_approved_by_viewer"]

    assert paid(PortalPrincipal(ANA, True, is_person=True)) == (True, True)
    assert paid(PortalPrincipal(ANA, True, is_person=False)) == (False, False)
