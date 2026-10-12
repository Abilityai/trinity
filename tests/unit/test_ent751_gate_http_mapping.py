"""
Gated skills — what an HTTP caller receives (trinity-enterprise#751).

Target: ``error_handlers.skill_gate_error`` and its registration in
``main.py``. Every route that reaches the gate lets the exception propagate;
the app-level handler turns it into the one shape: a 202 ``pending_approval``
body (never an empty 200 a client could read as a reply), or the refusal's own
status, both with the code on ``X-Trinity-Error-Code`` (#2889's header).
"""
import ast
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

pytestmark = pytest.mark.unit

_MAIN = Path(__file__).resolve().parents[2] / "src" / "backend" / "main.py"


@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from error_handlers import skill_gate_error
    from services.skill_gate_errors import SkillApprovalRequired, SkillGateError, SkillGateRefused

    app = FastAPI()
    app.add_exception_handler(SkillGateError, skill_gate_error)

    @app.post("/pending")
    def _pending():
        raise SkillApprovalRequired(request_id="gate-abc", agent_name="finance",
                                    skills=["pay-invoice"], approver_role="primary",
                                    expires_at="2026-10-03T10:00:00Z")

    @app.post("/refused")
    def _refused():
        raise SkillGateRefused(429, "approval_queue_full", "Ten are waiting.", limit=10)

    return TestClient(app)


def test_a_gated_call_answers_202_pending_approval(client):
    res = client.post("/pending")
    assert res.status_code == 202
    assert res.headers["X-Trinity-Error-Code"] == "approval_pending"
    body = res.json()
    assert body["status"] == "pending_approval"
    assert body["request_id"] == "gate-abc"
    assert body["skills"] == ["pay-invoice"]
    assert body["approver_role"] == "primary"
    assert "Not run" in body["message"]


def test_a_refusal_answers_its_own_status_and_code(client):
    res = client.post("/refused")
    assert res.status_code == 429
    assert res.headers["X-Trinity-Error-Code"] == "approval_queue_full"
    assert res.json()["detail"] == {"status": "refused", "code": "approval_queue_full",
                                    "message": "Ten are waiting.", "limit": 10}


def test_main_registers_the_handler_for_the_base_class():
    """The live consumer is the app's handler table (wiring, not behaviour)."""
    tree = ast.parse(_MAIN.read_text())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "add_exception_handler"]
    pairs = {(ast.unparse(c.args[0]), ast.unparse(c.args[1])) for c in calls if len(c.args) == 2}
    assert ("_SkillGateError", "_skill_gate_error") in pairs


def test_the_notice_names_no_role_and_promises_only_a_decision():
    """Eyeball finding: "needs approval from its primary" leaked a role id, and
    "the outcome will be sent" was untrue for requesters with no channel back."""
    from services.skill_gate_errors import SkillApprovalRequired

    e = SkillApprovalRequired(request_id="gate-x", agent_name="testfix", skills=["pay-invoice"],
                              approver_role="primary", expires_at=None, outcome_delivery="inbox")
    assert e.message == ("Not run: the skill pay-invoice on testfix needs approval before it "
                         "can run. Request gate-x is waiting for a decision.")
    assert e.detail()["approver_role"] == "primary"      # the role stays machine-readable


def test_a_requester_nobody_will_tell_is_told_so_and_where_the_outcome_shows():
    """trinity#3233: the default is "none" — an answer that does not know who
    will be told never promises a delivery."""
    from services.skill_gate_errors import SkillApprovalRequired

    e = SkillApprovalRequired(request_id="gate-x", agent_name="testfix", skills=["pay-invoice"],
                              approver_role="primary", expires_at=None)
    assert e.message == ("Not run: the skill pay-invoice on testfix needs approval before it "
                         "can run. Request gate-x is waiting for a decision. Nothing will be "
                         "sent back when it is decided: if it is approved, it runs as a new "
                         "execution on testfix; if not, nothing runs.")
    assert e.detail()["outcome_delivery"] == "none"
