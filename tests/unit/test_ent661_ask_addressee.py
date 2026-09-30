"""A platform raise can name exactly who is asked (ent#661).

Adding someone else's agent to a project needs THAT agent's owner. The default
`primary` addressing can't promise that: an assignment provider answers first
(the ent#606 ruling), and an owner with no email turns the ask into an
operator ask. So `raise_ask` takes `addressee=` — for a `gate` raise only. An
agent can still never choose who is asked.
"""
from __future__ import annotations

import pytest

from unit.test_ent611_native_ask import ask, real_db, _body  # noqa: F401 — fixtures

pytestmark = pytest.mark.unit

AGENT = "agent-661-addr"


def _gate(ask, rid, **kw):
    return ask.svc.raise_ask(AGENT, _body(rid, to="primary"), raised_by="gate", channel="gate", **kw)


def test_a_gate_raise_goes_to_the_named_person(ask):
    r = _gate(ask, "gate-661-a1", addressee="Owner.661@Example.com")
    row = ask.db.get_operator_queue_item(r["id"])
    assert r["resolved"] is True
    assert row["addressed_to_email"] == "owner.661@example.com"
    assert row["resolved_to"] == ["owner.661@example.com"]
    assert row["context"]["workspace_session_id"] == "thread-owner.661@example.com"


def test_the_named_person_wins_over_an_assignment_provider(ask):
    from services import assignment_provider

    class _P:
        def assignment_for(self, agent, trig):
            return None

        def people_for(self, agent_name, role):
            return {"emails": ["someone.else@example.com"]}

    assignment_provider.register_provider(_P())
    r = _gate(ask, "gate-661-a2", addressee="owner.661@example.com")
    assert ask.db.get_operator_queue_item(r["id"])["addressed_to_email"] == "owner.661@example.com"


def test_an_agent_may_not_name_who_is_asked(ask):
    with pytest.raises(ValueError):
        ask.svc.raise_ask(AGENT, _body("a661-1"), raised_by="agent", channel="mcp",
                          addressee="owner.661@example.com")
    assert ask.db.get_operator_queue_item_for_agent_by_request_id(AGENT, "a661-1") is None


@pytest.mark.parametrize("bad", ["", "   ", "not-an-email"])
def test_a_malformed_addressee_is_a_programming_error(ask, bad):
    with pytest.raises(ValueError):
        _gate(ask, "gate-661-bad", addressee=bad)


def test_without_an_addressee_the_default_addressing_is_unchanged(ask):
    r = _gate(ask, "gate-661-a3")
    row = ask.db.get_operator_queue_item(r["id"])
    assert row["addressed_to_email"] == ask.state["owner"]
