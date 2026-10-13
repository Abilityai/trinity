"""
Gated skills — gate asks have their own budget (trinity-enterprise#751).

Decided at the #751 plan gate (posted on the issue): a gate raise no longer
spends the EXECUTOR's ask budget. Shared, any requester could park the agent's
25 pending slots with gated requests and block the agent's own asks for a day;
and an agent flooding its own queue would block every gated request to it. The
gate enforces its own caps (per requester and per executor) before it raises.

Targets: ``services/ask_service.raise_ask`` (rate + depth for ``raised_by``)
and ``db/operator_queue`` (the per-agent pending counts the cap and the file
poller read), over the real per-process SQLite.
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

pytestmark = pytest.mark.unit

OWNER = "owner-751@example.com"


@pytest.fixture
def ask(monkeypatch):
    import json as _json
    from types import SimpleNamespace
    from database import db as real_db
    import services.ask_service as svc
    import services.operator_queue_service as oqs
    from services import assignment_provider
    from services.rate_limiter import RateLimitResult

    state = {"rate_ok": True}

    class _Audit:
        async def log(self, **kw):
            return "evt"

    class _WS:
        async def broadcast(self, message):
            _json.loads(message)

    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_owner_email", lambda agent: OWNER)
    monkeypatch.setattr(oqs, "_workspace_attachment", lambda agent, email, **_: (None, False))
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda agent: False, raising=False)
    monkeypatch.setattr(oqs.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(state["rate_ok"], 10, 0, 60))
    monkeypatch.setattr(oqs, "OPERATOR_QUEUE_MAX_PENDING_PER_AGENT", 2)
    assignment_provider.clear_provider()
    yield SimpleNamespace(svc=svc, db=real_db, state=state)
    assignment_provider.clear_provider()


def _body(request_id, **over):
    body = {"request_id": request_id, "type": "approval", "title": "Approve?",
            "question": "Run it?", "options": ["Approve", "Reject"], "to": "primary"}
    body.update(over)
    return body


def _agent_raise(ask, agent, rid):
    return ask.svc.raise_ask(agent, _body(rid), raised_by="agent", channel="mcp")


def _gate_raise(ask, agent, rid):
    return ask.svc.raise_ask(agent, _body(rid), raised_by="gate", channel="gate")


@pytest.mark.asyncio
async def test_a_gate_raise_is_not_refused_by_the_agents_full_queue(ask):
    agent = "agent-751-budget-1"
    _agent_raise(ask, agent, "own-1")
    _agent_raise(ask, agent, "own-2")
    with pytest.raises(ask.svc.AskRejected) as info:
        _agent_raise(ask, agent, "own-3")
    assert info.value.code == "queue_full"
    assert _gate_raise(ask, agent, "gate-b1-1")["status"] == "created"


@pytest.mark.asyncio
async def test_gate_asks_do_not_fill_the_agents_own_queue(ask):
    agent = "agent-751-budget-2"
    for i in range(3):
        assert _gate_raise(ask, agent, f"gate-b2-{i}")["status"] == "created"
    assert _agent_raise(ask, agent, "own-1")["status"] == "created"


@pytest.mark.asyncio
async def test_a_gate_raise_does_not_spend_the_agents_rate_bucket(ask):
    agent = "agent-751-budget-3"
    ask.state["rate_ok"] = False
    with pytest.raises(ask.svc.AskRejected) as info:
        _agent_raise(ask, agent, "own-1")
    assert info.value.code == "rate_limited"
    assert _gate_raise(ask, agent, "gate-b3-1")["status"] == "created"


@pytest.mark.asyncio
async def test_the_file_pollers_pending_count_leaves_gate_asks_out(ask):
    agent = "agent-751-budget-4"
    _agent_raise(ask, agent, "own-1")
    _gate_raise(ask, agent, "gate-b4-1")
    _gate_raise(ask, agent, "gate-b4-2")
    assert ask.db.count_operator_queue_pending_for_agent(agent) == 1
