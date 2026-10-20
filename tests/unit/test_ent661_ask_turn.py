"""An ask records the turn that raised it, as the platform saw it (ent#661 v3).

A project shows the asks raised in its chats and rooms, which needs the
raising turn. An MCP raise carried none: `operator_queue.execution_id` came
only from the agent-written `context.execution_id`, and an agent chatting in
a project chat usually writes none. The MCP tool now forwards the
platform-supplied `X-Trinity-Execution-Id` (#2392). When that id is one of
this agent's own executions it is stamped on the ask, winning over whatever
the agent wrote; otherwise nothing changes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from unit.test_ent611_native_ask import ask, real_db, _body  # noqa: F401 — fixtures

pytestmark = pytest.mark.unit

AGENT = "agent-661-turn"


@pytest.fixture()
def turns(monkeypatch):
    """Executions by id; only this agent's own resolve (the #2392 rule)."""
    import services.idempotency_service as idem
    table = {"exec-mine": SimpleNamespace(id="exec-mine", agent_name=AGENT),
             "exec-other": SimpleNamespace(id="exec-other", agent_name="someone-else")}
    monkeypatch.setattr(idem, "resolve_and_validate_execution",
                        lambda eid, agent: table.get(eid) if table.get(eid) and table[eid].agent_name == agent else None)
    return table


def _raise(ask, rid, context=None, **kw):
    body = _body(rid)
    if context is not None:
        body["context"] = context
    return ask.svc.raise_ask(AGENT, body, raised_by="agent", channel="mcp", **kw)


def test_the_platform_turn_is_stamped(ask, turns):
    r = _raise(ask, "t661-1", platform_execution_id="exec-mine")
    assert ask.db.get_operator_queue_item(r["id"])["execution_id"] == "exec-mine"


def test_the_platform_turn_wins_over_the_agents(ask, turns):
    r = _raise(ask, "t661-2", context={"execution_id": "exec-typed"}, platform_execution_id="exec-mine")
    assert ask.db.get_operator_queue_item(r["id"])["execution_id"] == "exec-mine"


@pytest.mark.parametrize("platform", [None, "manual", "exec-other", "exec-unknown"])
def test_an_unusable_platform_id_changes_nothing(ask, turns, platform):
    r = _raise(ask, f"t661-3-{platform}", context={"execution_id": "exec-typed"}, platform_execution_id=platform)
    assert ask.db.get_operator_queue_item(r["id"])["execution_id"] == "exec-typed"
