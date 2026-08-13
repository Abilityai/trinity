"""The agent page builds ONE card, not the whole roster (#2160).

ent#360's router got the page's identity and capabilities by calling
`get_roster` and picking one card out of it. Correct — it kept the page and the
sidebar from disagreeing about what an agent can do — and expensive in a way
that does not show up on a small fleet:

`get_roster` fans `_agent_briefing` across EVERY agent (a Docker lookup plus up
to two agent HTTP calls each, 5s timeout, awaited with `gather`). So opening one
agent's page paid for N briefings, and its load time was bounded by the slowest
agent in the fleet rather than by the agent being opened. One wedged agent meant
a five-second page for an unrelated one.

Profiled before fixing (recorded on the issue): the six `build_page` sub-reads
total 32.7 ms against a 1600-execution agent, while the endpoint took ~150 ms.
The gap was the roster.

So what is pinned here is the COUNT, not a duration — a timing assertion would
pass on a two-agent fixture no matter how the code was written, which is exactly
how this shipped.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-2160.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-2160-logs"))

import pytest

pytestmark = pytest.mark.unit

EMAIL = "alice@example.com"
FLEET = [f"agent-{i}" for i in range(12)]


def _rows(names):
    return [{"agent_name": n, "owner": "alice", "avatar_updated_at": None,
             "is_default_avatar": 1, "tts_voice_id": None, "shared_at": None}
            for n in names]


@pytest.fixture()
def svc(monkeypatch):
    from client_portal import service as s

    briefed = []

    async def counting_briefing(name):
        briefed.append(name)
        return ("desc", [])

    monkeypatch.setattr(s.db, "get_shared_roster", lambda e: _rows(FLEET))
    monkeypatch.setattr(s.db, "get_owned_roster", lambda e: [])
    monkeypatch.setattr(s, "_agent_briefing", counting_briefing)

    import services.tts_service as tts
    monkeypatch.setattr(tts, "is_available", lambda: False)
    return s, briefed


def test_one_agents_page_costs_one_briefing(svc):
    """The fix. Twelve agents in the fleet, one briefing issued."""
    s, briefed = svc

    card = asyncio.run(s.get_agent_card(EMAIL, "agent-3"))

    assert card is not None and card.name == "agent-3"
    assert briefed == ["agent-3"]


def test_the_roster_still_briefs_everyone(svc):
    """The comparison that makes the number above meaningful — and the roster's
    own behaviour is deliberately unchanged (its cards all need briefings)."""
    s, briefed = svc

    asyncio.run(s.get_roster(EMAIL))

    assert len(briefed) == len(FLEET)


def test_the_cost_does_not_grow_with_the_fleet(svc, monkeypatch):
    """The property that actually matters: a bigger fleet must not make one
    agent's page slower. A count taken on a 2-agent fixture would have passed
    against the original code too."""
    s, briefed = svc
    monkeypatch.setattr(s.db, "get_shared_roster", lambda e: _rows([f"a{i}" for i in range(200)]))

    asyncio.run(s.get_agent_card(EMAIL, "a137"))

    assert briefed == ["a137"]


def test_an_agent_off_the_roster_yields_no_card_and_no_briefing(svc):
    """The caller has already gated access, so None means "vanished between the
    two reads" — and an agent we will not render must not be contacted."""
    s, briefed = svc

    assert asyncio.run(s.get_agent_card(EMAIL, "not-mine")) is None
    assert briefed == []


def test_owned_agents_are_reachable_only_for_a_platform_session(svc, monkeypatch):
    """Membership must resolve by the SAME rule as the roster (ent#357): an
    external client sees only what was shared, a platform session also sees what
    it owns. A second rule here would let the page reach an agent the sidebar
    does not list."""
    s, _ = svc
    monkeypatch.setattr(s.db, "get_shared_roster", lambda e: [])
    monkeypatch.setattr(s.db, "get_owned_roster", lambda e: _rows(["mine"]))

    assert asyncio.run(s.get_agent_card(EMAIL, "mine", include_owned=False)) is None
    assert asyncio.run(s.get_agent_card(EMAIL, "mine", include_owned=True)) is not None


def test_the_card_is_built_by_the_same_helper_as_the_roster():
    """Both paths go through `_row_to_card`, so the page and the sidebar cannot
    drift on avatar/owner/voice — the reason ent#360 projected the roster card in
    the first place. That intent is preserved; only the fan-out is gone."""
    import inspect
    from client_portal import service as s

    assert "_row_to_card(" in inspect.getsource(s.get_agent_card)
    assert "_row_to_card(" in inspect.getsource(s.get_roster)


def test_the_router_no_longer_builds_the_whole_roster():
    """Guards the actual regression: a future edit reaching for `get_roster`
    here reintroduces the fleet-wide fan-out with no visible symptom on a small
    instance."""
    import inspect
    from client_portal import router as r

    src = inspect.getsource(r.portal_agent_page)
    assert "get_agent_card(" in src
    assert "get_roster(" not in src
