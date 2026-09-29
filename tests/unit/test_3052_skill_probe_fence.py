"""#3052 — `can_manage_agent_skills`: the non-raising form of the ent#596 fence.

`GET /api/agents/{name}/skill-sets?probe=true` runs one in-container exec. The
read itself is open to anyone who can see the agent, so the probe is honoured
only for a principal who would pass the set WRITES' fence
(`get_skill_managed_agent_by_name`): the skills-manage capability, then the
owner fence (owner or admin, never a connector key). Everyone else reads
`unknown`. The predicate must agree with that fence on every principal shape,
and must fail CLOSED on the shapes the fence refuses.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import dependencies  # noqa: E402

AGENT = "acme-bot"


def _user(**over):
    base = dict(id=1, username="alice", email="a@example.com", role="user",
                mcp_scope=None, agent_name=None, connector_agent=None, mcp_key_id=None)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def fake_db(monkeypatch):
    state = SimpleNamespace(sharers={("alice", AGENT)}, capable=set())
    monkeypatch.setattr(dependencies.db, "can_user_share_agent",
                        lambda username, agent: (username, agent) in state.sharers, raising=False)
    monkeypatch.setattr(dependencies.db, "agent_has_capability",
                        lambda agent, cap: agent in state.capable, raising=False)
    return state


def test_the_owner_may_probe(fake_db):
    assert dependencies.can_manage_agent_skills(_user(), AGENT) is True


def test_a_shared_reader_may_not(fake_db):
    assert dependencies.can_manage_agent_skills(_user(username="bob"), AGENT) is False


def test_a_connector_key_may_not_even_on_its_own_agent(fake_db):
    assert dependencies.can_manage_agent_skills(_user(connector_agent=AGENT), AGENT) is False


def test_an_agent_key_without_the_capability_may_not(fake_db):
    u = _user(mcp_scope="agent", agent_name="orchestrator")
    assert dependencies.can_manage_agent_skills(u, AGENT) is False


def test_an_agent_key_with_the_capability_still_meets_the_owner_fence(fake_db):
    fake_db.capable.add("orchestrator")
    assert dependencies.can_manage_agent_skills(_user(mcp_scope="agent", agent_name="orchestrator"), AGENT) is True
    stranger = _user(username="mallory", mcp_scope="agent", agent_name="orchestrator")
    assert dependencies.can_manage_agent_skills(stranger, AGENT) is False


def test_a_principal_with_no_mcp_scope_attribute_fails_closed(fake_db):
    u = _user()
    del u.mcp_scope
    assert dependencies.can_manage_agent_skills(u, AGENT) is False


def test_an_unknown_future_scope_fails_closed(fake_db):
    assert dependencies.can_manage_agent_skills(_user(mcp_scope="portal_delegate"), AGENT) is False


def test_it_never_raises_and_never_audits(fake_db, monkeypatch):
    """A downgraded read is not a refused write: no 403, no audit row."""
    import services.platform_audit_service as pas
    calls = []
    monkeypatch.setattr(pas.platform_audit_service, "log", lambda *a, **k: calls.append(k), raising=False)
    assert dependencies.can_manage_agent_skills(_user(username="bob", mcp_scope="agent", agent_name="x"), AGENT) is False
    assert calls == []
