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


def _rows(names, **extra):
    return [{"agent_name": n, "owner": "alice", "avatar_updated_at": None,
             "is_default_avatar": 1, "tts_voice_id": None, "shared_at": None,
             **extra}
            for n in names]


@pytest.fixture()
def svc(monkeypatch):
    from client_portal import service as s

    counted = {"batch": 0, "single": 0}

    briefed = []

    # #2196 gave `_agent_briefing` a second parameter: the container state the
    # caller already resolved (it used to make that Docker call itself, per card,
    # and discard the answer). A 1-arg stub here would raise TypeError inside
    # `get_roster`'s list comprehension — synchronously, OUTSIDE `gather`, so the
    # whole roster call would raise rather than degrade.
    async def counting_briefing(name, availability="ready"):
        briefed.append(name)
        return ("desc", [])

    monkeypatch.setattr(s.db, "get_shared_roster", lambda e: _rows(FLEET))
    monkeypatch.setattr(s.db, "get_owned_roster", lambda e: [])
    monkeypatch.setattr(s, "_agent_briefing", counting_briefing)

    # #2196: pin the container-state seams. `docker.from_env()` runs at import
    # and conftest re-imports that module after every test, so on a machine with
    # Docker up these would answer for real and the fixture's agents — which have
    # no containers — would all read "unavailable". Stubbed here so the counts
    # below stay about briefings, and the availability assertions stay about the
    # code rather than about the developer's daemon.
    async def _map(names):
        counted["batch"] += 1
        return {n: "ready" for n in names}

    async def _one(name):
        counted["single"] += 1
        return "ready"

    monkeypatch.setattr(s, "_availability_map", _map)
    monkeypatch.setattr(s, "_agent_availability", _one)

    import services.tts_service as tts
    monkeypatch.setattr(tts, "is_available", lambda: False)
    return s, briefed, counted


def test_one_agents_page_costs_one_briefing(svc):
    """The fix. Twelve agents in the fleet, one briefing issued."""
    s, briefed, _counted = svc

    card = asyncio.run(s.get_agent_card(EMAIL, "agent-3"))

    assert card is not None and card.name == "agent-3"
    assert briefed == ["agent-3"]


def test_the_roster_briefs_nobody_now(svc):
    """#2163 INVERTED this. It used to read `len(briefed) == len(FLEET)` — the
    comparison that made the number above meaningful — because the roster's
    fan-out was deliberately unchanged by #2160.

    That fan-out is exactly what made the Workspace's first paint bound to the
    slowest agent in the fleet, so the roster now awaits no briefing at all and
    the client hydrates through `GET /briefings`. The agent page's count above
    is unaffected: it was never the roster's cost that #2160 removed, it was
    the page paying it.
    """
    s, briefed, _counted = svc

    roster = asyncio.run(s.get_roster(EMAIL))

    assert briefed == []
    assert all(c.briefing_state == "pending" for c in roster.agents)


def test_the_cost_does_not_grow_with_the_fleet(svc, monkeypatch):
    """The property that actually matters: a bigger fleet must not make one
    agent's page slower. A count taken on a 2-agent fixture would have passed
    against the original code too."""
    s, briefed, _counted = svc
    monkeypatch.setattr(s.db, "get_shared_roster", lambda e: _rows([f"a{i}" for i in range(200)]))

    asyncio.run(s.get_agent_card(EMAIL, "a137"))

    assert briefed == ["a137"]


def test_an_agent_off_the_roster_yields_no_card_and_no_briefing(svc):
    """The caller has already gated access, so None means "vanished between the
    two reads" — and an agent we will not render must not be contacted."""
    s, briefed, _counted = svc

    assert asyncio.run(s.get_agent_card(EMAIL, "not-mine")) is None
    assert briefed == []


def test_owned_agents_are_reachable_only_for_a_platform_session(svc, monkeypatch):
    """Membership must resolve by the SAME rule as the roster (ent#357): an
    external client sees only what was shared, a platform session also sees what
    it owns. A second rule here would let the page reach an agent the sidebar
    does not list."""
    s, _briefed, _counted = svc
    monkeypatch.setattr(s.db, "get_shared_roster", lambda e: [])
    monkeypatch.setattr(s.db, "get_owned_roster", lambda e: _rows(["mine"]))

    assert asyncio.run(s.get_agent_card(EMAIL, "mine", include_owned=False)) is None
    assert asyncio.run(s.get_agent_card(EMAIL, "mine", include_owned=True)) is not None


def test_every_row_field_survives_the_extraction(svc, monkeypatch):
    """Field-level, not just "same helper" (#2159 × #2160).

    These two PRs collided on exactly this block: #2159 added
    `display_label=r.get("display_label")` to the inline card construction, and
    #2160 replaced that whole construction with `_row_to_card`. Git reports a
    conflict, and the naive resolution — take the refactor — drops the field
    silently. Nothing else would notice: the payload field is Optional so it
    still validates, and the frontend falls back to the slug, so the only
    symptom is #2159's bug quietly returning as every row rendering its slug
    again.

    The identity test below pins that both paths call one helper; it cannot
    catch a field missing from that helper. This pins the field.
    """
    s, _briefed, _counted = svc
    labelled = _rows(["agent-3"], display_label="Due Diligence")
    monkeypatch.setattr(s.db, "get_shared_roster", lambda e: labelled)

    page_card = asyncio.run(s.get_agent_card(EMAIL, "agent-3"))
    roster_card = asyncio.run(s.get_roster(EMAIL)).agents[0]

    assert page_card.display_label == "Due Diligence"
    assert roster_card.display_label == "Due Diligence"
    # #2196's field rides the same builder and is subject to exactly the same
    # silent-drop hazard the docstring above describes.
    assert page_card.availability == "ready"
    assert roster_card.availability == "ready"


def test_an_unset_label_stays_none_rather_than_being_coalesced(svc):
    """NULL means "render the slug" and that decision belongs to the render site
    (ent#181). Coalescing to the slug here would make the two ends disagree
    about what unset means — the thing #2159 deliberately avoided."""
    s, _briefed, _counted = svc

    card = asyncio.run(s.get_agent_card(EMAIL, "agent-3"))

    assert card.display_label is None


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


def test_the_page_pays_a_single_container_read_not_the_fleet_one(svc):
    """#2196 rides the #2160 property rather than undoing it.

    The roster resolves container state for the whole set in ONE batch call; the
    page resolves ONE agent's with the single-agent form. Routing the page
    through the batch would put the fleet-scale read straight back, which is the
    cost this whole file exists to keep out.
    """
    s, _briefed, counted = svc

    asyncio.run(s.get_agent_card(EMAIL, "agent-3"))

    assert counted == {"batch": 0, "single": 1}


def test_a_roster_load_reads_docker_once_for_the_whole_set(svc):
    """A structural count, not a duration: a per-card read would pass any timing
    assertion on a small fixture and cost N inspects on a real fleet."""
    s, _briefed, counted = svc

    asyncio.run(s.get_roster(EMAIL))

    assert counted == {"batch": 1, "single": 0}
