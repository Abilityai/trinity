"""#2213 — the roster must ship every client-visible skill the `/` popup can search.

Measured on a live instance before writing this, with 30 probe skills planted in a
real agent container:

    agent-server GET /api/skills   33 skills, all user_invocable
    roster payload playbooks[]     24            <- _MAX_BRIEFING_HINTS
    popup, query "probe 27"        0 matches, and nothing on screen saying why

The hint-card bound is not wrong for cards (#2101: a card grid has a layout limit,
and cards carry descriptions). It is wrong as the SEARCH corpus, because search
cannot reach what never shipped — and the popup, having no way to know, renders a
short list that looks complete.

So the two surfaces get two bounds, and the count before either is published:

  * `playbooks`             — unchanged, 24, rich (title/description/starter)
  * `searchable_playbooks`  — same set, 200, title + starter only
  * `playbooks_total`       — the true count, so truncation is visible not silent

What these tests pin is the SEPARATION and the honesty, not the numbers: a bound
may be retuned, but the searchable set must never be smaller than the card set, and
`playbooks_total` must never under-report.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _skills(n: int) -> list[dict]:
    return [{"name": f"skill-{i:03d}", "user_invocable": True} for i in range(1, n + 1)]


def _briefing(skill_count: int, *, allow_list=None):
    """Run the real `_agent_briefing` with the agent HTTP + connector reads stubbed."""
    from client_portal import service

    info = MagicMock(status_code=200)
    info.json.return_value = {"description": "d", "use_cases": []}
    skills = MagicMock(status_code=200)
    skills.json.return_value = {"skills": _skills(skill_count)}

    client = MagicMock()
    client.get = AsyncMock(side_effect=lambda url, **kw: info if "template/info" in url else skills)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    core_db = MagicMock()
    core_db.get_connector_config.return_value = (
        {"exposed_playbooks": allow_list} if allow_list is not None else None
    )

    with patch("services.agent_auth.agent_httpx_client", return_value=ctx), \
         patch("database.db", core_db):
        return asyncio.run(service._agent_briefing("atlas", "ready"))


def test_the_card_bound_still_binds_the_cards():
    """#2101's bound is untouched: the grid surface is not what changed."""
    from client_portal import service

    _desc, cards, _searchable, _total = _briefing(33)
    assert len(cards) == service._MAX_BRIEFING_HINTS == 24


def test_search_ships_the_whole_client_visible_set():
    """The reported bug: skill 27 of 33 existed and could not be found."""
    _desc, cards, searchable, total = _briefing(33)

    assert total == 33
    assert len(searchable) == 33
    titles = {p.title for p in searchable}
    assert "Skill 027" in titles
    # ...and it is precisely what the card list omitted.
    assert "Skill 027" not in {p.title for p in cards}


def test_searchable_is_never_smaller_than_the_card_set():
    """The invariant that survives retuning either bound."""
    # Review finding: this used to short-circuit `n == 0` to a hand-written tuple,
    # so the empty-skills path — the one where the use-case fallback tier takes over —
    # was never actually exercised.
    for n in (0, 1, 6, 23, 24, 25, 33, 210):
        _desc, cards, searchable, _total = _briefing(n)
        assert len(searchable) >= len(cards), n


def test_the_search_bound_is_reported_rather_than_hidden():
    """Past the search bound the list is still truncated — but `playbooks_total`
    makes it sayable, which is the whole difference from the original bug."""
    from client_portal import service

    _desc, _cards, searchable, total = _briefing(210)
    assert len(searchable) == service._MAX_SEARCHABLE_PLAYBOOKS == 200
    assert total == 210, "the count must be the truth, not the shipped length"
    assert total - len(searchable) == 10


def test_search_entries_carry_what_a_pick_needs_and_nothing_more():
    """`starterFor()` needs `starter_prompt`; the popup row needs `title`. Dropping
    `description` is what makes a 200-entry list cheap — ~40 chars, not ~540."""
    _desc, _cards, searchable, _total = _briefing(5)

    for p in searchable:
        assert p.title
        assert p.starter_prompt.startswith("/")
        assert p.description is None


def test_the_exposure_filter_still_decides_what_is_client_visible():
    """Search widened the payload; it did not widen the POLICY. An allow-list still
    bounds both surfaces, and a non-invocable skill still reaches neither."""
    _desc, cards, searchable, total = _briefing(33, allow_list=["skill-001", "skill-002"])

    assert total == 2
    assert len(searchable) == 2
    assert len(cards) == 2
    assert {p.title for p in searchable} == {"Skill 001", "Skill 002"}


def test_a_failed_briefing_degrades_to_empty_not_to_a_crash():
    """The roster must never fail for a briefing; the four-tuple shape holds."""
    from client_portal import service

    with patch("services.agent_auth.agent_httpx_client", side_effect=RuntimeError("boom")):
        result = asyncio.run(service._agent_briefing("atlas", "ready"))
    # Emptiness by FIELD, not `== (None, [], [], 0)`: the NamedTuple's defaults are
    # immutable `()` (a shared mutable class-level default would be aliased by every
    # failed card, in a module that mutates hint lists in place — review finding), and
    # what the contract owes the caller is "nothing", not a particular container type.
    assert result.description is None
    assert not result.playbooks and not result.searchable_playbooks
    assert result.playbooks_total == 0


def test_a_stopped_agent_is_not_probed_at_all():
    """Availability gating is unchanged — and still returns the wider tuple."""
    from client_portal import service

    result = asyncio.run(service._agent_briefing("atlas", "stopped"))
    assert result.description is None
    assert not result.playbooks and not result.searchable_playbooks
    assert result.playbooks_total == 0


# ---------------------------------------------------------------------------
# Review findings (second round)
# ---------------------------------------------------------------------------

def _long_skills(n: int, title_len: int = 4000):
    return [{"name": "x" * title_len + f"-{i}", "user_invocable": True} for i in range(n)]


def test_searchable_entries_obey_the_same_field_caps_as_cards():
    """Round one took the copies BEFORE `_bound_briefing_hints` applied its caps, so
    up to 200 uncapped agent-controlled strings shipped per card per roster load —
    and `name` comes from YAML frontmatter the agent author writes."""
    from client_portal import service

    info = MagicMock(status_code=200)
    info.json.return_value = {"description": "d", "use_cases": []}
    skills = MagicMock(status_code=200)
    skills.json.return_value = {"skills": _long_skills(30)}
    client = MagicMock()
    client.get = AsyncMock(side_effect=lambda url, **kw: info if "template/info" in url else skills)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    core_db = MagicMock()
    core_db.get_connector_config.return_value = None

    with patch("services.agent_auth.agent_httpx_client", return_value=ctx), \
         patch("database.db", core_db):
        _desc, cards, searchable, _total = asyncio.run(service._agent_briefing("a", "ready"))

    assert searchable, "nothing shipped — the test would be vacuous"
    for p in searchable:
        assert len(p.title) <= service._MAX_HINT_TITLE_CHARS
        assert len(p.starter_prompt) <= service._MAX_HINT_STARTER_CHARS
    for p in cards:
        assert len(p.title) <= service._MAX_HINT_TITLE_CHARS


def test_total_counts_the_use_case_tier_before_its_own_cap():
    """`_use_case_hints` truncates to 6, and round one counted AFTER it — so an agent
    declaring 10 use-cases reported 0 hidden while 4 were dropped: the same
    "looks complete" bug, one tier over."""
    from client_portal import service

    info = MagicMock(status_code=200)
    info.json.return_value = {"description": "d",
                              "use_cases": [f"use case {i}" for i in range(10)]}
    skills = MagicMock(status_code=200)
    skills.json.return_value = {"skills": []}          # forces the fallback tier
    client = MagicMock()
    client.get = AsyncMock(side_effect=lambda url, **kw: info if "template/info" in url else skills)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    core_db = MagicMock()
    core_db.get_connector_config.return_value = None

    with patch("services.agent_auth.agent_httpx_client", return_value=ctx), \
         patch("database.db", core_db):
        _desc, cards, _searchable, total = asyncio.run(service._agent_briefing("a", "ready"))

    assert len(cards) == service._MAX_USE_CASE_HINTS == 6
    assert total == 10, "the hidden 4 must be countable, not invisible"


def test_the_briefing_defaults_are_not_a_shared_mutable():
    """A NamedTuple's defaults are CLASS-level. `[]` would mean every failed card
    aliases one list — in a module whose `_bound_briefing_hints` mutates hints in
    place."""
    from client_portal.service import AgentBriefing

    a, b = AgentBriefing(), AgentBriefing()
    assert a.playbooks is b.playbooks           # same object...
    assert not isinstance(a.playbooks, list)    # ...but immutable, so aliasing is safe
    with pytest.raises((AttributeError, TypeError)):
        a.playbooks.append("x")
